/* aja-capture — AJA NTV2 capture helper for pjcap
 *
 * Captures raw video+audio frames from an AJA device and streams them
 * to stdout for pjcap.py to consume and encode via AVAssetWriter.
 *
 * Protocol:
 *   1. First line on stdout: JSON header with signal info
 *   2. Then repeated: [4B BE video_size][video_data][4B BE audio_size][audio_data]
 *
 * Control: "stop\n" on stdin triggers graceful shutdown.
 * All status/errors go to stderr.
 */

#include "ntv2democommon.h"
#include "ntv2devicescanner.h"
#include "ntv2utils.h"
#include "ntv2devicefeatures.h"
#include "ajabase/system/process.h"
#include "ajabase/system/thread.h"
#include "ajabase/common/timecodeburn.h"

#include <csignal>
#include <cstring>
#include <getopt.h>
#include <iostream>
#include <sstream>
#include <unistd.h>
#include <poll.h>

using namespace std;

static const ULWord kAppSignature = NTV2_FOURCC('p','j','a','j');
static const unsigned CIRC_BUFFER_SIZE = 10;

static volatile bool gQuit = false;

static void SignalHandler(int) { gQuit = true; }

// ── Helpers ────────────────────────────────────────────────────────

static bool WriteAll(int fd, const void *buf, size_t len)
{
    const uint8_t *p = static_cast<const uint8_t*>(buf);
    while (len > 0) {
        ssize_t n = ::write(fd, p, len);
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;  // EPIPE or other error
        }
        p += n;
        len -= size_t(n);
    }
    return true;
}

static bool WriteBE32(int fd, uint32_t val)
{
    uint8_t buf[4];
    buf[0] = uint8_t(val >> 24);
    buf[1] = uint8_t(val >> 16);
    buf[2] = uint8_t(val >> 8);
    buf[3] = uint8_t(val);
    return WriteAll(fd, buf, 4);
}

struct PixelFormatEntry {
    const char *name;
    NTV2PixelFormat fmt;
};

static const PixelFormatEntry kPixelFormats[] = {
    {"8BitYCbCr",   NTV2_FBF_8BIT_YCBCR},
    {"10BitYCbCr",  NTV2_FBF_10BIT_YCBCR},
    {"8BitBGRA",    NTV2_FBF_ABGR},
    {"10BitRGB",    NTV2_FBF_10BIT_DPX},
    {nullptr,       NTV2_FBF_INVALID},
};

static NTV2PixelFormat ParsePixelFormat(const string &name)
{
    for (const auto &e : kPixelFormats)
        if (e.name && name == e.name)
            return e.fmt;
    return NTV2_FBF_INVALID;
}

static const char *PixelFormatName(NTV2PixelFormat fmt)
{
    for (const auto &e : kPixelFormats)
        if (e.name && e.fmt == fmt)
            return e.name;
    return "unknown";
}

// ── Capture Engine ────────────────────────────────────────────────

class AJACaptureHelper
{
public:
    AJACaptureHelper(const string &deviceSpec, int channel,
                     NTV2PixelFormat pixFmt, bool withAudio,
                     const string &inputSpec = "")
        : mDeviceSpec(deviceSpec)
        , mInputChannel(NTV2Channel(channel - 1))
        , mPixelFormat(pixFmt)
        , mWithAudio(withAudio)
        , mInputSpec(inputSpec)
        , mAudioSystem(withAudio ? NTV2_AUDIOSYSTEM_1 : NTV2_AUDIOSYSTEM_INVALID)
        , mVideoFormat(NTV2_FORMAT_UNKNOWN)
        , mSavedTaskMode(NTV2_DISABLE_TASKS)
        , mDeviceID(DEVICE_ID_NOTFOUND)
    {
    }

    ~AJACaptureHelper()
    {
        Stop();
        mDevice.UnsubscribeInputVerticalEvent(mInputChannel);
        mDevice.UnsubscribeOutputVerticalEvent(NTV2_CHANNEL1);
    }

    bool Init()
    {
        // Open device
        if (!CNTV2DeviceScanner::GetFirstDeviceFromArgument(mDeviceSpec, mDevice)) {
            cerr << "## ERROR: Device '" << mDeviceSpec << "' not found" << endl;
            return false;
        }
        if (!mDevice.IsDeviceReady()) {
            cerr << "## ERROR: '" << mDevice.GetDisplayName() << "' not ready" << endl;
            return false;
        }
        mDeviceID = mDevice.GetDeviceID();
        if (!mDevice.features().CanDoCapture()) {
            cerr << "## ERROR: '" << mDevice.GetDisplayName() << "' is playback-only" << endl;
            return false;
        }
        if (!mDevice.features().CanDoFrameBufferFormat(mPixelFormat)) {
            cerr << "## ERROR: '" << mDevice.GetDisplayName() << "' doesn't support pixel format "
                 << ::NTV2FrameBufferFormatToString(mPixelFormat, true) << endl;
            return false;
        }

        // Acquire device
        mDevice.GetTaskMode(mSavedTaskMode);
        if (!mDevice.AcquireStreamForApplication(kAppSignature, int32_t(AJAProcess::GetPid()))) {
            cerr << "## ERROR: Unable to acquire '" << mDevice.GetDisplayName()
                 << "' — another application owns it" << endl;
            return false;
        }
        mDevice.SetTaskMode(NTV2_OEM_TASKS);

        if (mDevice.features().CanDoMultiFormat())
            mDevice.SetMultiFormatMode(false);

        // Determine input source
        if (!mInputSpec.empty()) {
            // Explicit input: hdmi, hdmi1, sdi, sdi1, sdi2, etc.
            string spec = mInputSpec;
            for (auto &c : spec) c = tolower(c);
            if (spec == "hdmi" || spec == "hdmi1")
                mInputSource = NTV2_INPUTSOURCE_HDMI1;
            else if (spec == "hdmi2")
                mInputSource = NTV2_INPUTSOURCE_HDMI2;
            else if (spec == "hdmi3")
                mInputSource = NTV2_INPUTSOURCE_HDMI3;
            else if (spec == "hdmi4")
                mInputSource = NTV2_INPUTSOURCE_HDMI4;
            else if (spec.substr(0, 3) == "sdi")
                mInputSource = ::NTV2ChannelToInputSource(mInputChannel);
            else {
                cerr << "## ERROR: Unknown input '" << mInputSpec
                     << "' — use hdmi, hdmi1-4, or sdi" << endl;
                return false;
            }
        } else {
            // Auto-detect: try HDMI first (if device has HDMI inputs), then SDI
            bool foundSignal = false;
            if (mDevice.features().GetNumHDMIVideoInputs() > 0) {
                mInputSource = NTV2_INPUTSOURCE_HDMI1;
                NTV2VideoFormat fmt = mDevice.GetInputVideoFormat(mInputSource);
                if (fmt != NTV2_FORMAT_UNKNOWN) {
                    cerr << "Auto-detected signal on HDMI1" << endl;
                    foundSignal = true;
                }
            }
            if (!foundSignal) {
                mInputSource = ::NTV2ChannelToInputSource(mInputChannel);
                NTV2VideoFormat fmt = mDevice.GetInputVideoFormat(mInputSource);
                if (fmt != NTV2_FORMAT_UNKNOWN) {
                    cerr << "Auto-detected signal on " << ::NTV2InputSourceToString(mInputSource, true) << endl;
                    foundSignal = true;
                }
            }
            if (!foundSignal) {
                // Fall through — SetupVideo will report "No input signal"
                mInputSource = ::NTV2ChannelToInputSource(mInputChannel);
            }
        }
        cerr << "Using input: " << ::NTV2InputSourceToString(mInputSource, true) << endl;

        // For HDMI input, use channel 1 framestores
        if (NTV2_INPUT_SOURCE_IS_HDMI(mInputSource))
            mInputChannel = NTV2_CHANNEL1;

        // Determine active SDIs/inputs and framestores for 4K
        const bool is12G = mDevice.features().CanDo12gRouting();
        const UWord numSpigots = is12G ? 1 : 2;  // TSI uses 2 for non-12G
        mDoTSI = !is12G;
        mActiveSDIs = ::NTV2MakeChannelSet(::NTV2InputSourceToChannel(mInputSource), numSpigots);
        mActiveFrameStores = ::NTV2MakeChannelSet(mInputChannel, numSpigots);

        if (!SetupVideo()) return false;
        if (mWithAudio && !SetupAudio()) return false;
        SetupHostBuffers();
        if (!RouteInputSignal()) return false;

        return true;
    }

    bool EmitHeader()
    {
        ULWord fpsNum = 0, fpsDen = 0;
        NTV2FrameRate frameRate = ::GetNTV2FrameRateFromVideoFormat(mVideoFormat);
        ::GetFramesPerSecond(frameRate, fpsNum, fpsDen);

        UWord numAudioCh = 0;
        if (mWithAudio)
            numAudioCh = mDevice.features().GetMaxAudioChannels();

        ostringstream json;
        json << "{"
             << "\"width\":" << mFormatDesc.GetRasterWidth()
             << ",\"height\":" << mFormatDesc.GetVisibleRasterHeight()
             << ",\"fps_num\":" << fpsNum
             << ",\"fps_den\":" << fpsDen
             << ",\"pixel_format\":\"" << PixelFormatName(mPixelFormat) << "\""
             << ",\"audio_channels\":" << numAudioCh
             << ",\"audio_sample_rate\":48000"
             << ",\"video_format\":\"" << ::NTV2VideoFormatToString(mVideoFormat) << "\""
             << "}" << endl;

        string hdr = json.str();
        return WriteAll(STDOUT_FILENO, hdr.c_str(), hdr.size());
    }

    void Run()
    {
        mAVCircularBuffer.SetAbortFlag(const_cast<bool*>(reinterpret_cast<volatile bool*>(&gQuit)));

        // Start threads
        mProducerThread.Attach(ProducerStatic, this);
        mProducerThread.SetPriority(AJA_ThreadPriority_High);
        mProducerThread.Start();

        mConsumerThread.Attach(ConsumerStatic, this);
        mConsumerThread.SetPriority(AJA_ThreadPriority_High);
        mConsumerThread.Start();
    }

    void Stop()
    {
        gQuit = true;
        while (mProducerThread.Active())
            AJATime::Sleep(10);
        while (mConsumerThread.Active())
            AJATime::Sleep(10);
        mDevice.ReleaseStreamForApplication(kAppSignature, int32_t(AJAProcess::GetPid()));
        mDevice.SetTaskMode(mSavedTaskMode);
    }

    void GetStatus(ULWord &good, ULWord &dropped, ULWord &level)
    {
        AUTOCIRCULATE_STATUS st;
        mDevice.AutoCirculateGetStatus(mInputChannel, st);
        good = st.GetProcessedFrameCount();
        dropped = st.GetDroppedFrameCount();
        level = st.GetBufferLevel();
    }

private:
    // ── Video setup ──
    bool SetupVideo()
    {
        mDevice.EnableChannels(mActiveFrameStores, true);
        mDevice.EnableInputInterrupt(mInputChannel);
        mDevice.SubscribeInputVerticalEvent(mInputChannel);
        mDevice.SubscribeOutputVerticalEvent(NTV2_CHANNEL1);

        if (mDevice.features().HasBiDirectionalSDI()
            && NTV2_INPUT_SOURCE_IS_SDI(mInputSource)) {
            mDevice.SetSDITransmitEnable(mActiveSDIs, false);
            mDevice.WaitForOutputVerticalInterrupt(NTV2_CHANNEL1, 10);
        }

        mVideoFormat = mDevice.GetInputVideoFormat(mInputSource);
        if (mVideoFormat == NTV2_FORMAT_UNKNOWN) {
            cerr << "## ERROR: No input signal or unknown format" << endl;
            return false;
        }
        // Convert to 4K format if applicable
        CNTV2DemoCommon::Get4KInputFormat(mVideoFormat);
        mFormatDesc = NTV2FormatDescriptor(mVideoFormat, mPixelFormat);

        mDevice.SetReference(NTV2_REFERENCE_FREERUN);
        mDevice.SetVideoFormat(mVideoFormat, false, false, mInputChannel);
        mDevice.SetVANCMode(mActiveFrameStores, NTV2_VANCMODE_OFF);

        if (mDevice.features().CanDo12gRouting())
            ;  // 12G: TSI mux built into framestores
        else if (mDoTSI)
            mDevice.SetTsiFrameEnable(true, mInputChannel);
        else
            mDevice.Set4kSquaresEnable(true, mInputChannel);

        mDevice.SetFrameBufferFormat(mActiveFrameStores, mPixelFormat);

        cerr << "Signal: " << ::NTV2VideoFormatToString(mVideoFormat)
             << " " << mFormatDesc.GetRasterWidth() << "x"
             << mFormatDesc.GetVisibleRasterHeight()
             << " " << ::NTV2FrameBufferFormatToString(mPixelFormat, true) << endl;
        return true;
    }

    // ── Audio setup ──
    bool SetupAudio()
    {
        CaptureConfig cfg(mDeviceSpec);
        cfg.fInputSource = mInputSource;
        cfg.fInputChannel = mInputChannel;
        NTV2AudioSystemSet audSystems(::NTV2MakeAudioSystemSet(mAudioSystem, 1));
        CNTV2DemoCommon::ConfigureAudioSystems(mDevice, cfg, audSystems);
        return true;
    }

    // ── Signal routing ──
    bool RouteInputSignal()
    {
        NTV2LHIHDMIColorSpace inputColorSpace(NTV2_LHIHDMIColorSpaceYCbCr);
        if (NTV2_INPUT_SOURCE_IS_HDMI(mInputSource))
            mDevice.GetHDMIInputColor(inputColorSpace, mInputChannel);

        const bool isInputRGB(inputColorSpace == NTV2_LHIHDMIColorSpaceRGB);
        NTV2XptConnections connections;

        CaptureConfig cfg(mDeviceSpec);
        cfg.fInputChannel = mInputChannel;
        cfg.fInputSource = mInputSource;
        cfg.fPixelFormat = mPixelFormat;
        cfg.fDoTSIRouting = mDoTSI;

        bool ok;
        if (NTV2_IS_4K_VIDEO_FORMAT(mVideoFormat))
            ok = CNTV2DemoCommon::GetInputRouting4K(connections, cfg, mDeviceID, isInputRGB);
        else
            ok = CNTV2DemoCommon::GetInputRouting(connections, cfg, isInputRGB);

        if (!ok) {
            cerr << "## ERROR: Failed to compute input routing" << endl;
            return false;
        }
        return mDevice.ApplySignalRoute(connections, true);
    }

    // ── Host buffers ──
    void SetupHostBuffers()
    {
        const size_t audioSize = NTV2_IS_VALID_AUDIO_SYSTEM(mAudioSystem)
                                     ? NTV2_AUDIOSIZE_MAX : 0;
        mHostBuffers.reserve(CIRC_BUFFER_SIZE);
        while (mHostBuffers.size() < CIRC_BUFFER_SIZE) {
            mHostBuffers.push_back(NTV2FrameData());
            NTV2FrameData &fd = mHostBuffers.back();
            fd.fVideoBuffer.Allocate(mFormatDesc.GetVideoWriteSize());
            fd.fAudioBuffer.Allocate(audioSize);
            mAVCircularBuffer.Add(&fd);
            if (fd.fVideoBuffer)
                mDevice.DMABufferLock(fd.fVideoBuffer, true);
            if (fd.fAudioBuffer)
                mDevice.DMABufferLock(fd.fAudioBuffer, true);
        }
    }

    // ── Producer thread (DMA capture) ──
    static void ProducerStatic(AJAThread *, void *ctx)
    {
        static_cast<AJACaptureHelper*>(ctx)->ProducerLoop();
    }

    void ProducerLoop()
    {
        AUTOCIRCULATE_TRANSFER inputXfer;
        ULWord acOptions = AUTOCIRCULATE_WITH_RP188;

        // Frame range for TSI
        static const UWord startFrame[] = {0, 7, 14, 21};
        NTV2ACFrameRange frames(7);
        if (mDevice.features().CanDo12gRouting())
            frames.setRangeWithCount(7, 0);
        else
            frames.setRangeWithCount(7, startFrame[mInputChannel / 2]);

        mDevice.AutoCirculateStop(mActiveFrameStores);
        if (!mDevice.AutoCirculateInitForInput(mInputChannel, frames, mAudioSystem, acOptions)) {
            cerr << "## ERROR: AutoCirculateInitForInput failed" << endl;
            gQuit = true;
            return;
        }
        if (!mDevice.AutoCirculateStart(mInputChannel)) {
            cerr << "## ERROR: AutoCirculateStart failed" << endl;
            gQuit = true;
            return;
        }

        cerr << "Capture started" << endl;

        while (!gQuit) {
            AUTOCIRCULATE_STATUS acStatus;
            mDevice.AutoCirculateGetStatus(mInputChannel, acStatus);

            if (acStatus.IsRunning() && acStatus.HasAvailableInputFrame()) {
                NTV2FrameData *pFD = mAVCircularBuffer.StartProduceNextBuffer();
                if (!pFD) continue;

                inputXfer.SetVideoBuffer(pFD->VideoBuffer(), pFD->VideoBufferSize());
                if (acStatus.WithAudio())
                    inputXfer.SetAudioBuffer(pFD->AudioBuffer(), pFD->AudioBufferSize());

                mDevice.AutoCirculateTransfer(mInputChannel, inputXfer);

                if (acStatus.WithAudio())
                    pFD->fNumAudioBytes = inputXfer.GetCapturedAudioByteCount();

                mAVCircularBuffer.EndProduceNextBuffer();
            } else {
                mDevice.WaitForInputVerticalInterrupt(mInputChannel);
            }
        }

        mDevice.AutoCirculateStop(mInputChannel);
        cerr << "Capture stopped" << endl;
    }

    // ── Consumer thread (stdout writer) ──
    static void ConsumerStatic(AJAThread *, void *ctx)
    {
        static_cast<AJACaptureHelper*>(ctx)->ConsumerLoop();
    }

    void ConsumerLoop()
    {
        while (!gQuit) {
            NTV2FrameData *pFD = mAVCircularBuffer.StartConsumeNextBuffer();
            if (!pFD) continue;

            uint32_t videoSize = uint32_t(pFD->VideoBufferSize());
            uint32_t audioSize = uint32_t(pFD->fNumAudioBytes);

            if (!WriteBE32(STDOUT_FILENO, videoSize)
                || !WriteAll(STDOUT_FILENO, pFD->VideoBuffer(), videoSize)
                || !WriteBE32(STDOUT_FILENO, audioSize)
                || (audioSize > 0
                    && !WriteAll(STDOUT_FILENO, pFD->AudioBuffer(), audioSize)))
            {
                // Pipe broken — pjcap.py closed its end
                gQuit = true;
                mAVCircularBuffer.EndConsumeNextBuffer();
                break;
            }

            mAVCircularBuffer.EndConsumeNextBuffer();
        }
    }

    // ── Members ──
    string              mDeviceSpec;
    string              mInputSpec;
    CNTV2Card           mDevice;
    NTV2DeviceID        mDeviceID;
    NTV2Channel         mInputChannel;
    NTV2InputSource     mInputSource;
    NTV2PixelFormat     mPixelFormat;
    NTV2VideoFormat     mVideoFormat;
    NTV2FormatDesc      mFormatDesc;
    NTV2TaskMode        mSavedTaskMode;
    NTV2AudioSystem     mAudioSystem;
    bool                mWithAudio;
    bool                mDoTSI;

    NTV2ChannelSet      mActiveSDIs;
    NTV2ChannelSet      mActiveFrameStores;

    AJAThread           mProducerThread;
    AJAThread           mConsumerThread;
    NTV2FrameDataArray  mHostBuffers;
    FrameDataRingBuffer mAVCircularBuffer;
};

// ── List devices ──────────────────────────────────────────────────

static void ListDevices()
{
    CNTV2Card device;
    bool found = false;
    for (ULWord idx = 0; CNTV2DeviceScanner::GetDeviceAtIndex(idx, device); idx++) {
        if (!found) {
            cerr << "AJA Devices:" << endl;
            found = true;
        }
        cerr << "  " << idx << ": " << device.GetDisplayName() << endl;
    }
    if (!found)
        cerr << "No AJA devices found." << endl;
}

// ── Main ──────────────────────────────────────────────────────────

static void Usage(const char *prog)
{
    cerr << "Usage: " << prog << " [options]" << endl
         << "  -d, --device SPEC     Device index, serial, or model (default: 0)" << endl
         << "  -i, --input SOURCE    Input: hdmi, hdmi1-4, sdi (default: auto-detect)" << endl
         << "  -c, --channel N       Input channel 1-8 (default: 1)" << endl
         << "  -p, --pixel-format F  Pixel format (default: 8BitYCbCr)" << endl
         << "      --no-audio        Disable audio capture" << endl
         << "      --list            List available devices and exit" << endl
         << "  -h, --help            Show this help" << endl
         << endl
         << "Pixel formats: 8BitYCbCr, 10BitYCbCr, 8BitBGRA, 10BitRGB" << endl;
}

int main(int argc, char **argv)
{
    string deviceSpec = "0";
    string inputSpec;
    int channel = 1;
    string pixFmtStr = "8BitYCbCr";
    bool withAudio = true;
    bool listDevices = false;

    static struct option longOpts[] = {
        {"device",       required_argument, nullptr, 'd'},
        {"input",        required_argument, nullptr, 'i'},
        {"channel",      required_argument, nullptr, 'c'},
        {"pixel-format", required_argument, nullptr, 'p'},
        {"no-audio",     no_argument,       nullptr, 'A'},
        {"list",         no_argument,       nullptr, 'L'},
        {"help",         no_argument,       nullptr, 'h'},
        {nullptr, 0, nullptr, 0}
    };

    int opt;
    while ((opt = getopt_long(argc, argv, "d:i:c:p:h", longOpts, nullptr)) != -1) {
        switch (opt) {
        case 'd': deviceSpec = optarg; break;
        case 'i': inputSpec = optarg; break;
        case 'c': channel = atoi(optarg); break;
        case 'p': pixFmtStr = optarg; break;
        case 'A': withAudio = false; break;
        case 'L': listDevices = true; break;
        case 'h': Usage(argv[0]); return 0;
        default:  Usage(argv[0]); return 2;
        }
    }

    if (listDevices) {
        ListDevices();
        return 0;
    }

    if (channel < 1 || channel > 8) {
        cerr << "## ERROR: Channel must be 1-8, got " << channel << endl;
        return 2;
    }

    NTV2PixelFormat pixFmt = ParsePixelFormat(pixFmtStr);
    if (!NTV2_IS_VALID_FRAME_BUFFER_FORMAT(pixFmt)) {
        cerr << "## ERROR: Unknown pixel format '" << pixFmtStr << "'" << endl;
        cerr << "Valid formats:";
        for (const auto &e : kPixelFormats)
            if (e.name) cerr << " " << e.name;
        cerr << endl;
        return 2;
    }

    ::signal(SIGINT, SignalHandler);
    ::signal(SIGTERM, SignalHandler);
    ::signal(SIGHUP, SignalHandler);
    ::signal(SIGPIPE, SIG_IGN);  // Handle EPIPE in write() instead

    AJACaptureHelper capture(deviceSpec, channel, pixFmt, withAudio, inputSpec);
    if (!capture.Init())
        return 1;

    if (!capture.EmitHeader())
        return 1;

    capture.Run();

    // Wait for "stop" on stdin or signal
    struct pollfd pfd;
    pfd.fd = STDIN_FILENO;
    pfd.events = POLLIN;

    char line[256];
    while (!gQuit) {
        int ret = ::poll(&pfd, 1, 250);
        if (ret > 0 && (pfd.revents & POLLIN)) {
            if (::fgets(line, sizeof(line), stdin)) {
                if (strncmp(line, "stop", 4) == 0)
                    break;
            } else {
                break;  // stdin closed
            }
        }
    }

    capture.Stop();

    ULWord good, dropped, level;
    capture.GetStatus(good, dropped, level);
    cerr << "Frames captured: " << good << ", dropped: " << dropped << endl;

    return 0;
}
