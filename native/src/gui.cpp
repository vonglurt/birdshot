// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// PreviewPump: the idle capture loop every front end shares.
// Viewfinder: the `birdshot gui` loopback HTTP server -- one accept
// thread, one short-lived thread per request, frames from the pump.
// Recording happens in neither: it stays with the Engine.
//
// The server binds loopback only, on purpose: this is a viewfinder for the
// person at the machine, not a network camera. Exposing it further is a
// decision someone should have to make in code, not in config.
#include "birdshot/gui.hpp"

#include <chrono>
#include <csignal>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <thread>

#include "birdshot/analysis.hpp"
#include "birdshot/backend.hpp"
#include "birdshot/config.hpp"
#include "birdshot/exposure.hpp"
#include "birdshot/jpeg.hpp"
#include "birdshot/json.hpp"
#include "birdshot/solar.hpp"
#include "birdshot/version.hpp"

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
using sock_t = SOCKET;
static void close_sock(sock_t s) { closesocket(s); }
static bool sock_valid(sock_t s) { return s != INVALID_SOCKET; }
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>
using sock_t = int;
static void close_sock(sock_t s) { ::close(s); }
static bool sock_valid(sock_t s) { return s >= 0; }
#endif

namespace bs {

namespace {

double mono_now() {
  using namespace std::chrono;
  return duration<double>(steady_clock::now().time_since_epoch()).count();
}

double unix_now() {
  using namespace std::chrono;
  return duration<double>(system_clock::now().time_since_epoch()).count();
}

}  // namespace

// ------------------------------------------------------------- the pump --

PreviewPump::PreviewPump(Config& cfg, Backend& backend) : cfg_(cfg), backend_(backend) {}

PreviewPump::~PreviewPump() { stop(); }

void PreviewPump::start(Sink sink) {
  stop();
  sink_ = std::move(sink);
  stopped_.store(false);
  thread_ = std::thread(&PreviewPump::loop, this);
}

void PreviewPump::stop() {
  stopped_.store(true);
  if (thread_.joinable()) thread_.join();
}

// The engine's loop, minus storage: capture, analyse, let AE steer. AE gets
// virtual frame-cadence time (seq * 0.25) exactly as the engine feeds it --
// the PID gains are tuned for the 1.x frame interval, and wall-clock dt at
// preview rates makes the d-term amplify metering noise.
void PreviewPump::loop() {
  ExposureController ae(cfg_);
  ae.set_limits(backend_.limits());

  const bool auto_exposure = cfg_.boolean("auto_exposure", true);
  int64_t exposure_us = static_cast<int64_t>(
      auto_exposure ? cfg_.state("last_shutter_us").number(2000)
                    : cfg_.num("manual_shutter_us", 2000));
  double gain = auto_exposure ? cfg_.state("last_gain").number(1.0)
                              : cfg_.num("manual_gain", 1.0);
  if (exposure_us <= 0) exposure_us = 2000;
  if (gain < kGainMin) gain = kGainMin;

  {
    const Frame probe = backend_.capture(exposure_us, gain);
    if (auto seeded = ae.seed(probe.lux)) {
      exposure_us = seeded->first;
      gain = seeded->second;
    }
  }

  const double fps_cap = cfg_.num("gui_preview_fps", 12.0);
  const double frame_due = fps_cap > 0 ? 1.0 / fps_cap : 0.0;

  int64_t seq = 0;
  double fps = 0.0;
  double last = mono_now();

  while (!stopped_.load()) {
    Frame frame = backend_.capture(exposure_us, gain);
    ++seq;

    const FrameStats st = analyse(frame.y, cfg_, frame.y.centre_crop(512));
    ExposureDecision dec;
    if (auto_exposure) {
      dec = ae.update(st, frame.exposure_us, frame.gain, frame.lux, seq * 0.25);
      exposure_us = dec.exposure_us;
      gain = dec.gain;
    } else {
      dec.exposure_us = frame.exposure_us;
      dec.gain = frame.gain;
      dec.meter = st.meter;
      dec.target = ae.target_luma();
      dec.settled = true;
      dec.mode = "manual";
    }

    const double now = mono_now();
    const double dt = now - last;
    last = now;
    // Skip the first sample: its dt is capture time alone, before any
    // cadence sleep, and it would take the EMA seconds to live it down.
    if (dt > 0 && seq > 1) fps = fps == 0.0 ? 1.0 / dt : 0.9 * fps + 0.1 / dt;

    if (sink_) sink_(frame, st, dec, fps);

    const double spent = mono_now() - now;
    if (frame_due > spent)
      std::this_thread::sleep_for(std::chrono::duration<double>(frame_due - spent));
  }
  ae.persist();
}

// ------------------------------------------------------- the viewfinder --

namespace {

void sockets_init() {
#ifdef _WIN32
  static bool done = false;
  if (!done) {
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
    done = true;
  }
#else
  // A streamer writing to a tab that closed must get an error code back,
  // not kill the process.
  std::signal(SIGPIPE, SIG_IGN);
#endif
}

bool send_all(sock_t s, const void* data, size_t len) {
  const char* p = static_cast<const char*>(data);
  while (len > 0) {
    const auto n = ::send(s, p, static_cast<int>(len), 0);
    if (n <= 0) return false;
    p += n;
    len -= static_cast<size_t>(n);
  }
  return true;
}

bool send_str(sock_t s, const std::string& text) {
  return send_all(s, text.data(), text.size());
}

std::string http_head(const char* status, const char* type, size_t length) {
  char buf[256];
  std::snprintf(buf, sizeof(buf),
                "HTTP/1.1 %s\r\n"
                "Content-Type: %s\r\n"
                "Content-Length: %zu\r\n"
                "Cache-Control: no-store\r\n"
                "Connection: close\r\n\r\n",
                status, type, length);
  return buf;
}

// The whole page, in-tree like everything else. Dark, one column: the
// stream, a status strip fed by /status.json, a footer naming the build.
const char kPage[] = R"HTML(<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>birdshot</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background: #101214; color: #cfd6dd;
         font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
  header { padding: 14px 0 8px; letter-spacing: .18em; color: #8b949e; }
  header b { color: #e6edf3; font-weight: 600; }
  .frame { background: #000; border: 1px solid #21262d; border-radius: 6px;
           overflow: hidden; max-width: min(92vw, 960px); }
  .frame img { display: block; width: 100%; height: auto; }
  .strip { display: flex; flex-wrap: wrap; gap: 10px 22px; justify-content: center;
           padding: 12px 16px; max-width: min(92vw, 960px); }
  .cell { text-align: center; min-width: 72px; }
  .cell .k { color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
  .cell .v { color: #e6edf3; font-size: 15px; }
  #verdict.ok { color: #56d364; } #verdict.dark { color: #79c0ff; }
  #verdict.blown { color: #e3b341; } #verdict.empty { color: #8b949e; }
  footer { margin-top: auto; padding: 10px; color: #57606a; font-size: 12px; }
</style>
</head>
<body>
<header><b>birdshot</b> viewfinder</header>
<div class="frame"><img src="/stream" alt="live preview"></div>
<div class="strip">
  <div class="cell"><div class="k">verdict</div><div class="v" id="verdict">&mdash;</div></div>
  <div class="cell"><div class="k">shutter</div><div class="v" id="shutter">&mdash;</div></div>
  <div class="cell"><div class="k">gain</div><div class="v" id="gain">&mdash;</div></div>
  <div class="cell"><div class="k">lux</div><div class="v" id="lux">&mdash;</div></div>
  <div class="cell"><div class="k">meter</div><div class="v" id="meter">&mdash;</div></div>
  <div class="cell"><div class="k">ae</div><div class="v" id="ae">&mdash;</div></div>
  <div class="cell"><div class="k">fps</div><div class="v" id="fps">&mdash;</div></div>
  <div class="cell"><div class="k">sun</div><div class="v" id="sun">&mdash;</div></div>
</div>
<footer id="foot"></footer>
<script>
  const el = id => document.getElementById(id);
  const shutter = us => us >= 1e6 ? (us / 1e6).toFixed(1) + ' s'
                      : us >= 1000 ? '1/' + Math.round(1e6 / us)
                      : us + ' µs';
  async function tick() {
    try {
      const s = await (await fetch('/status.json')).json();
      el('verdict').textContent = s.verdict;
      el('verdict').className = s.verdict;
      el('shutter').textContent = shutter(s.exposure_us);
      el('gain').textContent = 'x' + s.gain.toFixed(2);
      el('lux').textContent = s.lux.toFixed(1);
      el('meter').textContent = s.meter.toFixed(0) + ' / ' + s.target.toFixed(0);
      el('ae').textContent = s.ae_mode + (s.settled ? ' ✓' : '');
      el('fps').textContent = s.fps.toFixed(1);
      el('sun').textContent = 'sun' in s.solar
          ? s.solar.sun.toFixed(1) + '°' : 'no site';
      el('foot').textContent = s.backend + ' ' + s.width + 'x' + s.height +
          ' · birdshot ' + s.version;
    } catch (e) { /* server going away; the stream freezing says enough */ }
  }
  tick();
  setInterval(tick, 1000);
</script>
</body>
</html>
)HTML";

}  // namespace

Viewfinder::Viewfinder(Config& cfg) : cfg_(cfg) {}

Viewfinder::~Viewfinder() { stop(); }

bool Viewfinder::start(int port, std::string* err) {
  sockets_init();

  const sock_t fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (!sock_valid(fd)) {
    if (err) *err = "could not create a socket";
    return false;
  }
  const int one = 1;
  ::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&one), sizeof(one));

  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  addr.sin_port = htons(static_cast<uint16_t>(port));
  if (::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 ||
      ::listen(fd, 8) != 0) {
    if (err) *err = "could not bind 127.0.0.1:" + std::to_string(port) +
                    " -- is another viewfinder running?";
    close_sock(fd);
    return false;
  }
  socklen_t alen = sizeof(addr);
  ::getsockname(fd, reinterpret_cast<sockaddr*>(&addr), &alen);
  port_ = ntohs(addr.sin_port);
  listen_fd_ = static_cast<intptr_t>(fd);

  stopping_.store(false);
  backend_ = make_backend(cfg_);
  pump_.reset(new PreviewPump(cfg_, *backend_));
  pump_->start([this](const Frame& f, const FrameStats& st, const ExposureDecision& d,
                      double fps) { on_frame(f, st, d, fps); });
  accept_thread_ = std::thread(&Viewfinder::accept_loop, this);
  return true;
}

void Viewfinder::stop() {
  if (stopping_.exchange(true)) return;
  frame_cv_.notify_all();
  if (listen_fd_ >= 0) close_sock(static_cast<sock_t>(listen_fd_));
  if (accept_thread_.joinable()) accept_thread_.join();
  if (pump_) pump_->stop();
  // Request threads are detached but must not outlive this object: they
  // check stopping_ at least every 500 ms, so this drains quickly.
  const double deadline = mono_now() + 2.0;
  while (clients_.load() > 0 && mono_now() < deadline)
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  listen_fd_ = -1;
}

void Viewfinder::on_frame(const Frame& frame, const FrameStats& st, const ExposureDecision& dec,
                          double fps) {
  Json status = Json::object();
  status["version"] = kVersion;
  status["backend"] = backend_->name();
  status["width"] = frame.y.w;
  status["height"] = frame.y.h;
  status["fps"] = fps;
  status["exposure_us"] = static_cast<long long>(frame.exposure_us);
  status["gain"] = frame.gain;
  status["lux"] = frame.lux;
  status["verdict"] = st.verdict;
  status["meter"] = st.meter;
  status["target"] = dec.target;
  status["ae_mode"] = dec.mode;
  status["settled"] = dec.settled;
  status["clip_hi"] = st.clip_hi;
  status["sharpness"] = st.sharpness_norm;
  Json solar = Json::object();
  if (cfg_.boolean("site_set", false)) {
    const Site site = cfg_.site();
    const SunPos sun = sun_position(unix_now(), site.lat_deg, site.lon_deg);
    solar["sun"] = sun.elevation_deg;
    solar["azimuth"] = sun.azimuth_deg;
  }
  status["solar"] = solar;

  std::vector<uint8_t> jpg =
      encode_jpeg(frame.y, static_cast<int>(cfg_.num("gui_jpeg_quality", 85)));
  {
    std::lock_guard<std::mutex> lock(mu_);
    jpeg_ = std::move(jpg);
    status_json_ = status.dump();
    ++generation_;
  }
  frame_cv_.notify_all();
}

void Viewfinder::accept_loop() {
  const sock_t fd = static_cast<sock_t>(listen_fd_);
  while (!stopping_.load()) {
    fd_set rfds;
    FD_ZERO(&rfds);
    FD_SET(fd, &rfds);
    timeval tv{0, 500 * 1000};
    const int ready = ::select(static_cast<int>(fd) + 1, &rfds, nullptr, nullptr, &tv);
    if (stopping_.load()) break;
    if (ready <= 0) continue;
    const sock_t client = ::accept(fd, nullptr, nullptr);
    if (!sock_valid(client)) continue;
    std::thread(&Viewfinder::serve, this, static_cast<intptr_t>(client)).detach();
  }
}

void Viewfinder::serve(intptr_t client_fd) {
  const sock_t client = static_cast<sock_t>(client_fd);
  struct Count {
    std::atomic<int>& n;
    ~Count() { --n; }
  } count{clients_};
  ++clients_;

  char buf[2048];
  const auto n = ::recv(client, buf, sizeof(buf) - 1, 0);
  if (n <= 0) {
    close_sock(client);
    return;
  }
  buf[n] = '\0';
  std::string path = "/";
  {
    // "GET /path HTTP/1.1" -- anything malformed just gets the page.
    const char* sp1 = std::strchr(buf, ' ');
    const char* sp2 = sp1 ? std::strchr(sp1 + 1, ' ') : nullptr;
    if (sp1 && sp2) path.assign(sp1 + 1, sp2);
  }

  if (path == "/" || path == "/index.html") {
    const std::string body(kPage);
    send_str(client, http_head("200 OK", "text/html; charset=utf-8", body.size()));
    send_str(client, body);
  } else if (path == "/status.json") {
    std::string body;
    {
      std::lock_guard<std::mutex> lock(mu_);
      body = status_json_;
    }
    if (body.empty()) body = "{}";
    send_str(client, http_head("200 OK", "application/json", body.size()));
    send_str(client, body);
  } else if (path == "/frame.jpg") {
    std::vector<uint8_t> jpg;
    {
      std::lock_guard<std::mutex> lock(mu_);
      jpg = jpeg_;
    }
    if (jpg.empty()) {
      const std::string body = "warming up\n";
      send_str(client, http_head("503 Service Unavailable", "text/plain", body.size()));
      send_str(client, body);
    } else {
      send_str(client, http_head("200 OK", "image/jpeg", jpg.size()));
      send_all(client, jpg.data(), jpg.size());
    }
  } else if (path == "/stream") {
    send_str(client,
             "HTTP/1.1 200 OK\r\n"
             "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
             "Cache-Control: no-store\r\nConnection: close\r\n\r\n");
    uint64_t seen = 0;
    while (!stopping_.load()) {
      std::vector<uint8_t> jpg;
      {
        std::unique_lock<std::mutex> lock(mu_);
        frame_cv_.wait_for(lock, std::chrono::milliseconds(500),
                           [&] { return stopping_.load() || generation_ != seen; });
        if (stopping_.load() || generation_ == seen) continue;
        seen = generation_;
        jpg = jpeg_;
      }
      char part[128];
      std::snprintf(part, sizeof(part),
                    "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %zu\r\n\r\n",
                    jpg.size());
      if (!send_str(client, part) || !send_all(client, jpg.data(), jpg.size()) ||
          !send_str(client, "\r\n"))
        break;  // the tab closed
    }
  } else {
    const std::string body = "not found\n";
    send_str(client, http_head("404 Not Found", "text/plain", body.size()));
    send_str(client, body);
  }
  close_sock(client);
}

// ------------------------------------------------------------------------

namespace {

std::atomic<bool> g_gui_stop{false};
void gui_signal(int) { g_gui_stop.store(true); }

void open_in_browser(const std::string& url) {
#if defined(_WIN32)
  std::system(("start \"\" " + url).c_str());
#elif defined(__APPLE__)
  std::system(("open " + url + " >/dev/null 2>&1").c_str());
#else
  std::system(("xdg-open " + url + " >/dev/null 2>&1 &").c_str());
#endif
}

}  // namespace

int run_gui(Config& cfg, const GuiOptions& opts) {
  const int port = opts.port > 0 ? opts.port : static_cast<int>(cfg.num("gui_port", 8477));

  Viewfinder vf(cfg);
  std::string err;
  if (!vf.start(port, &err)) {
    std::fprintf(stderr, "%s\n", err.c_str());
    return 1;
  }

  const std::string url = "http://127.0.0.1:" + std::to_string(vf.port()) + "/";
  std::printf("viewfinder  %s\n", url.c_str());
  std::printf("Ctrl-C stops it.\n");
  if (opts.open_browser) open_in_browser(url);

  g_gui_stop.store(false);
  std::signal(SIGINT, gui_signal);
  std::signal(SIGTERM, gui_signal);
  while (!g_gui_stop.load())
    std::this_thread::sleep_for(std::chrono::milliseconds(200));

  std::printf("\nstopping\n");
  vf.stop();
  return 0;
}

}  // namespace bs
