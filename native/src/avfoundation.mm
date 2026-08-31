// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// The macOS camera backend: AVFoundation behind the same five calls as
// every other backend. The session delivers 420f frames on its own queue;
// capture() hands back the latest luma plane, letterboxed to the 640x480
// analysis geometry the whole pipeline shares. A webcam owns its own
// exposure, so the requested shutter/gain are accepted and ignored -- the
// missing "exposure" capability is what tells the GUI to grey those dials,
// exactly as the 1.x opencv backend did.
//
// Platform frameworks only (AVFoundation/CoreMedia/CoreVideo): a system
// boundary like the C++ standard library, not third-party code in-tree.
#include "backend_impl.hpp"

#import <AVFoundation/AVFoundation.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <memory>
#include <mutex>

#include "birdshot/config.hpp"

namespace bs {
namespace {

// Frames land here from the capture queue; capture() waits on the other
// side. Shared by pointer so the ObjC delegate can outlive nothing.
struct AvfShared {
  std::mutex mu;
  std::condition_variable cv;
  Gray8 latest;  // luma at the camera's delivered size
  uint64_t gen = 0;
};

}  // namespace
}  // namespace bs

@interface BsFrameSink : NSObject <AVCaptureVideoDataOutputSampleBufferDelegate> {
 @public
  std::shared_ptr<bs::AvfShared> shared;
}
@end

@implementation BsFrameSink
- (void)captureOutput:(AVCaptureOutput*)output
    didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer
           fromConnection:(AVCaptureConnection*)connection {
  CVImageBufferRef buf = CMSampleBufferGetImageBuffer(sampleBuffer);
  if (!buf || !shared) return;
  if (CVPixelBufferLockBaseAddress(buf, kCVPixelBufferLock_ReadOnly) != kCVReturnSuccess) return;
  const size_t w = CVPixelBufferGetWidthOfPlane(buf, 0);
  const size_t h = CVPixelBufferGetHeightOfPlane(buf, 0);
  const size_t stride = CVPixelBufferGetBytesPerRowOfPlane(buf, 0);
  const auto* base = static_cast<const uint8_t*>(CVPixelBufferGetBaseAddressOfPlane(buf, 0));
  if (base && w > 0 && h > 0) {
    std::lock_guard<std::mutex> lock(shared->mu);
    shared->latest = bs::Gray8(static_cast<int>(w), static_cast<int>(h));
    for (size_t row = 0; row < h; ++row)
      std::copy(base + row * stride, base + row * stride + w,
                shared->latest.px.begin() + static_cast<long>(row * w));
    ++shared->gen;
  }
  CVPixelBufferUnlockBaseAddress(buf, kCVPixelBufferLock_ReadOnly);
  if (base) shared->cv.notify_all();
}
@end

namespace bs {

namespace {

NSArray<AVCaptureDevice*>* discover_devices() {
  NSMutableArray<AVCaptureDeviceType>* types =
      [NSMutableArray arrayWithObject:AVCaptureDeviceTypeBuiltInWideAngleCamera];
  if (@available(macOS 14.0, *)) [types addObject:AVCaptureDeviceTypeExternal];
  AVCaptureDeviceDiscoverySession* session = [AVCaptureDeviceDiscoverySession
      discoverySessionWithDeviceTypes:types
                            mediaType:AVMediaTypeVideo
                             position:AVCaptureDevicePositionUnspecified];
  return session.devices;
}

// The permission dance. First use pops the system prompt; the completion
// is async, so wait for it (bounded) rather than racing the answer.
bool ensure_camera_access(std::string* err) {
  const AVAuthorizationStatus status =
      [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeVideo];
  if (status == AVAuthorizationStatusAuthorized) return true;
  if (status == AVAuthorizationStatusNotDetermined) {
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);
    __block BOOL granted = NO;
    [AVCaptureDevice requestAccessForMediaType:AVMediaTypeVideo
                             completionHandler:^(BOOL ok) {
                               granted = ok;
                               dispatch_semaphore_signal(sem);
                             }];
    dispatch_semaphore_wait(sem,
                            dispatch_time(DISPATCH_TIME_NOW, 60 * NSEC_PER_SEC));
    if (granted) return true;
  }
  if (err)
    *err = "camera access is not granted (System Settings > Privacy & Security > Camera)";
  return false;
}

class AvfBackend : public Backend {
 public:
  AvfBackend(AVCaptureDevice* device, std::string* err) {
    const char* name = [device.localizedName UTF8String];
    model_ = name ? name : "webcam";
    shared_ = std::make_shared<AvfShared>();

    session_ = [[AVCaptureSession alloc] init];
    if ([session_ canSetSessionPreset:AVCaptureSessionPreset1280x720])
      session_.sessionPreset = AVCaptureSessionPreset1280x720;

    NSError* nserr = nil;
    AVCaptureDeviceInput* input = [AVCaptureDeviceInput deviceInputWithDevice:device
                                                                        error:&nserr];
    if (!input || ![session_ canAddInput:input]) {
      if (err)
        *err = "could not open " + model_ +
               (nserr ? std::string(": ") + [nserr.localizedDescription UTF8String] : "");
      session_ = nil;
      return;
    }
    [session_ addInput:input];

    sink_ = [[BsFrameSink alloc] init];
    sink_->shared = shared_;
    AVCaptureVideoDataOutput* output = [[AVCaptureVideoDataOutput alloc] init];
    output.videoSettings = @{
      (id)kCVPixelBufferPixelFormatTypeKey :
          @(kCVPixelFormatType_420YpCbCr8BiPlanarFullRange)
    };
    output.alwaysDiscardsLateVideoFrames = YES;
    queue_ = dispatch_queue_create("birdshot.avf", DISPATCH_QUEUE_SERIAL);
    [output setSampleBufferDelegate:sink_ queue:queue_];
    if (![session_ canAddOutput:output]) {
      if (err) *err = "could not attach a video output to " + model_;
      session_ = nil;
      return;
    }
    [session_ addOutput:output];
    [session_ startRunning];
    ok_ = true;
  }

  ~AvfBackend() override {
    if (session_) [session_ stopRunning];
  }

  bool ok() const { return ok_; }

  std::string name() const override {
    std::lock_guard<std::mutex> lock(shared_->mu);
    if (shared_->latest.empty()) return "webcam " + model_;
    return "webcam " + model_ + " " + std::to_string(shared_->latest.w) + "x" +
           std::to_string(shared_->latest.h);
  }

  std::vector<std::string> capabilities() const override {
    // No "exposure": the device owns its own AE. No "rapid": a webcam
    // delivers at its own cadence and a flat-out loop gains nothing.
    return {"stills", "timelapse", "birdflight"};
  }

  SensorLimits limits() const override { return {}; }

  Frame capture(int64_t exposure_us, double gain) override {
    using namespace std::chrono;
    Frame frame;
    frame.exposure_us = exposure_us;  // accepted, ignored: the webcam decides
    frame.gain = gain;
    frame.ts = duration<double>(system_clock::now().time_since_epoch()).count();

    Gray8 native;
    {
      std::unique_lock<std::mutex> lock(shared_->mu);
      const uint64_t seen = shared_->gen;
      shared_->cv.wait_for(lock, seconds(2), [&] { return shared_->gen != seen; });
      if (shared_->gen == seen && shared_->latest.empty()) {
        // No frame ever arrived (permission revoked mid-run, device gone).
        if (!warned_) {
          std::fprintf(stderr, "webcam %s is not delivering frames\n", model_.c_str());
          warned_ = true;
        }
        frame.y = Gray8(640, 480, 0);
        return frame;
      }
      native = shared_->latest;
    }

    // Letterbox to the shared analysis geometry: scale to fit, centre,
    // black bars. Nearest neighbour -- this plane feeds metering and
    // gates, and 720p -> 640x360 keeps more detail than the gates need.
    Gray8 out(640, 480, 0);
    const double scale = std::min(640.0 / native.w, 480.0 / native.h);
    const int dw = std::max(1, static_cast<int>(native.w * scale));
    const int dh = std::max(1, static_cast<int>(native.h * scale));
    const int ox = (640 - dw) / 2, oy = (480 - dh) / 2;
    for (int y = 0; y < dh; ++y) {
      const int sy = std::min(native.h - 1, static_cast<int>(y / scale));
      for (int x = 0; x < dw; ++x) {
        const int sx = std::min(native.w - 1, static_cast<int>(x / scale));
        out.at(ox + x, oy + y) = native.at(sx, sy);
      }
    }
    frame.y = std::move(out);
    return frame;
  }

 private:
  AVCaptureSession* session_ = nil;
  BsFrameSink* sink_ = nil;
  dispatch_queue_t queue_ = nil;
  std::shared_ptr<AvfShared> shared_;
  std::string model_;
  bool ok_ = false;
  bool warned_ = false;
};

}  // namespace

std::vector<CameraInfo> avf_list_cameras() {
  std::vector<CameraInfo> out;
  @autoreleasepool {
    NSArray<AVCaptureDevice*>* devices = discover_devices();
    int i = 0;
    for (AVCaptureDevice* d in devices) {
      const char* name = [d.localizedName UTF8String];
      out.push_back({"avfoundation", i++, name ? name : "webcam"});
    }
  }
  return out;
}

std::unique_ptr<Backend> make_avf_backend(const Config&, int index, std::string* err) {
  @autoreleasepool {
    if (!ensure_camera_access(err)) return nullptr;
    NSArray<AVCaptureDevice*>* devices = discover_devices();
    if (devices.count == 0) {
      if (err) *err = "no camera devices found";
      return nullptr;
    }
    const NSUInteger idx =
        std::min<NSUInteger>(static_cast<NSUInteger>(std::max(0, index)), devices.count - 1);
    auto backend = std::make_unique<AvfBackend>(devices[idx], err);
    if (!backend->ok()) return nullptr;
    return backend;
  }
}

}  // namespace bs
