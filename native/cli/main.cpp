// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Paul Richeson
//
// birdshot -- headless control, the planner, alignment and the selftest.
// One static binary per platform; `birdshot help` lists everything.
#include <atomic>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include "birdshot/align.hpp"
#include "birdshot/backend.hpp"
#include "birdshot/config.hpp"
#include "birdshot/engine.hpp"
#include "birdshot/geo.hpp"
#include "birdshot/jpeg.hpp"
#include "birdshot/naming.hpp"
#include "birdshot/plan.hpp"
#include "birdshot/selftest.hpp"
#include "birdshot/solar.hpp"
#include "birdshot/storage.hpp"
#include "birdshot/version.hpp"

namespace fs = std::filesystem;
using namespace bs;

namespace {

Engine* g_engine = nullptr;

void on_signal(int) {
  // Route power-downs and Ctrl-C through the normal shutdown: the engine
  // finishes the frame, closes the session and persists resume state.
  if (g_engine) g_engine->stop();
}

void install_signal_handlers() {
  std::signal(SIGINT, on_signal);
  std::signal(SIGTERM, on_signal);
#ifdef SIGHUP
  std::signal(SIGHUP, on_signal);
#endif
}

struct Args {
  std::vector<std::string> positional;
  std::string config_path;
  std::string site_arg;
  std::string date_arg;
  std::string out_path;
  std::string name_arg;
  double elev = 0.0;
  double tolerance = 0.25;
  double interval = -1.0;
  double focal = 0.0;
  long long count = -1;
  int days = 7;
  bool verbose = false;
  bool json = false;
};

bool parse_args(int argc, char** argv, int from, Args* out, std::string* err) {
  for (int i = from; i < argc; ++i) {
    const std::string a = argv[i];
    auto need_value = [&](const char* flag) -> const char* {
      if (i + 1 >= argc) {
        *err = std::string(flag) + " needs a value";
        return nullptr;
      }
      return argv[++i];
    };
    if (a == "-v" || a == "--verbose") out->verbose = true;
    else if (a == "--json") out->json = true;
    else if (a == "-n" || a == "--count") {
      const char* v = need_value("-n");
      if (!v) return false;
      out->count = std::atoll(v);
    } else if (a == "-i" || a == "--interval") {
      const char* v = need_value("-i");
      if (!v) return false;
      out->interval = std::atof(v);
    } else if (a == "--days") {
      const char* v = need_value("--days");
      if (!v) return false;
      out->days = std::atoi(v);
    } else if (a == "--site") {
      const char* v = need_value("--site");
      if (!v) return false;
      out->site_arg = v;
    } else if (a == "--date") {
      const char* v = need_value("--date");
      if (!v) return false;
      out->date_arg = v;
    } else if (a == "--config") {
      const char* v = need_value("--config");
      if (!v) return false;
      out->config_path = v;
    } else if (a == "--out") {
      const char* v = need_value("--out");
      if (!v) return false;
      out->out_path = v;
    } else if (a == "--name") {
      const char* v = need_value("--name");
      if (!v) return false;
      out->name_arg = v;
    } else if (a == "--elev") {
      const char* v = need_value("--elev");
      if (!v) return false;
      out->elev = std::atof(v);
    } else if (a == "--tolerance") {
      const char* v = need_value("--tolerance");
      if (!v) return false;
      out->tolerance = std::atof(v);
    } else if (a == "--focal") {
      const char* v = need_value("--focal");
      if (!v) return false;
      out->focal = std::atof(v);
    } else if (!a.empty() && a[0] == '-') {
      *err = "unknown option " + a;
      return false;
    } else {
      out->positional.push_back(a);
    }
  }
  return true;
}

// Unix timestamp (UTC noon) for "YYYY-MM-DD"; falls back to now.
double parse_date_or_now(const std::string& s) {
  int y = 0, m = 0, d = 0;
  if (std::sscanf(s.c_str(), "%d-%d-%d", &y, &m, &d) == 3 && m >= 1 && m <= 12 && d >= 1 &&
      d <= 31) {
    y -= m <= 2;
    const int era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = static_cast<unsigned>(y - era * 400);
    const unsigned doy = static_cast<unsigned>((153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1);
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    const long long days =
        static_cast<long long>(era) * 146097 + static_cast<long long>(doe) - 719468;
    return static_cast<double>(days) * 86400.0 + 43200.0;
  }
  return static_cast<double>(std::time(nullptr));
}

Site resolve_site(const Config& cfg, const Args& args, std::string* err) {
  Site site = cfg.site();
  if (!args.site_arg.empty()) {
    double lat = 0, lon = 0;
    if (!parse_latlon(args.site_arg, &lat, &lon)) {
      *err = "--site wants \"lat,lon\" in decimal degrees";
      return {};
    }
    site.lat_deg = lat;
    site.lon_deg = lon;
    site.elev_m = args.elev;
  } else if (!cfg.boolean("site_set", false)) {
    *err = "no site configured: run `birdshot site set <lat,lon>` or pass --site";
    return {};
  }
  return site;
}

std::string local_stamp(double unix_ts) {
  const time_t t = static_cast<time_t>(unix_ts);
  std::tm lt{};
#ifdef _WIN32
  localtime_s(&lt, &t);
#else
  localtime_r(&t, &lt);
#endif
  char buf[32];
  std::snprintf(buf, sizeof buf, "%04d-%02d-%02d %02d:%02d:%02d", lt.tm_year + 1900,
                lt.tm_mon + 1, lt.tm_mday, lt.tm_hour, lt.tm_min, lt.tm_sec);
  return buf;
}

int usage() {
  std::printf(
      "birdshot %s (%s) -- bird and sky capture, native\n"
      "\n"
      "usage: birdshot <command> [options]\n"
      "\n"
      "capture\n"
      "  capture    [-n N] [-v]        COLLECT: full pipeline with quality gates\n"
      "  rapid      [-n N]             flat centisecond names, fastest path\n"
      "  timelapse  [-i SEC] [-n N]    one frame every SEC seconds\n"
      "  birdflight [-n TAKES] [-v]    watch the sky; fire on a bird\n"
      "\n"
      "horizons\n"
      "  sun   [--site lat,lon] [--date YYYY-MM-DD]   position now + today's events\n"
      "  plan  [--days N] [--site lat,lon] [--focal MM] [--json]\n"
      "  align <dir>... [--tolerance DEG] [--out FILE.json]\n"
      "  site  set <lat,lon> [--name NAME] [--elev M] | show\n"
      "\n"
      "housekeeping\n"
      "  info | sessions | doctor | selftest | version\n"
      "\n"
      "  --config PATH overrides ~/.config/birdshot/settings.json everywhere\n",
      kVersion, kCodename);
  return 2;
}

int cmd_info(Config& cfg) {
  auto backend = make_backend(cfg);
  std::printf("birdshot %s (%s)\n", kVersion, kCodename);
  std::printf("config    %s\n", cfg.path().c_str());
  const std::string root = expand_user(cfg.str("data_root", "~/birdshot-data"));
  std::printf("data root %s (%llu MB free)\n", root.c_str(),
              static_cast<unsigned long long>(free_space_mb(fs::path(root).has_root_path()
                                                                ? root
                                                                : ".")));
  std::printf("backend   %s (", backend->name().c_str());
  const auto caps = backend->capabilities();
  for (size_t i = 0; i < caps.size(); ++i)
    std::printf("%s%s", i ? ", " : "", caps[i].c_str());
  std::printf(")\n");
  if (cfg.boolean("site_set", false)) {
    const Site s = cfg.site();
    std::printf("site      %s%s%s\n", format_latlon(s.lat_deg, s.lon_deg).c_str(),
                s.name.empty() ? "" : "  ", s.name.c_str());
  } else {
    std::printf("site      not set (birdshot site set <lat,lon>)\n");
  }
  const Json k = cfg.state("k_lux");
  std::printf("ae        k_lux %s, last shutter %s\n",
              k.is_number() ? std::to_string(k.number()).c_str() : "unlearned",
              describe_shutter(static_cast<int64_t>(cfg.state("last_shutter_us").number(2000)))
                  .c_str());
  return 0;
}

int cmd_sun(Config& cfg, const Args& args) {
  std::string err;
  const Site site = resolve_site(cfg, args, &err);
  if (!err.empty()) {
    std::fprintf(stderr, "%s\n", err.c_str());
    return 1;
  }
  const double when = args.date_arg.empty() ? static_cast<double>(std::time(nullptr))
                                            : parse_date_or_now(args.date_arg);
  const SunPos sp = sun_position(when, site.lat_deg, site.lon_deg);
  std::printf("site %s%s%s\n", format_latlon(site.lat_deg, site.lon_deg).c_str(),
              site.name.empty() ? "" : "  ", site.name.c_str());
  std::printf("at %s local:\n", local_stamp(when).c_str());
  std::printf("  elevation %+7.2f deg (apparent)   azimuth %6.2f deg\n", sp.elevation_deg,
              sp.azimuth_deg);
  std::printf("  declination %+6.2f deg   equation of time %+5.1f min\n", sp.declination_deg,
              sp.eqtime_min);

  struct Event {
    const char* label;
    double alt;
    bool rising;
  };
  const Event events[] = {
      {"astronomical dawn", kAltAstronomical, true}, {"nautical dawn", kAltNautical, true},
      {"civil dawn", kAltCivil, true},               {"sunrise", kAltSunset, true},
      {"golden hour", kAltGoldenHour, false},        {"lower-limb contact", kAltLowerLimbTouch, false},
      {"sunset", kAltSunset, false},                 {"civil dusk", kAltCivil, false},
      {"nautical dusk", kAltNautical, false},        {"astronomical dusk", kAltAstronomical, false},
  };
  std::printf("today:\n");
  for (const auto& e : events) {
    const auto t = sun_crossing(when, site.lat_deg, site.lon_deg, e.alt, e.rising);
    if (t)
      std::printf("  %-20s %s\n", e.label, local_stamp(*t).c_str());
    else
      std::printf("  %-20s does not occur at this latitude today\n", e.label);
  }
  const auto az = sunset_azimuth_deg(when, site.lat_deg, site.lon_deg);
  if (az) {
    std::printf("sunset azimuth %.1f deg; sun descending %.1f deg/h there\n", *az,
                std::fabs(altitude_rate_deg_per_hour(site.lat_deg, *az)));
  }
  return 0;
}

int cmd_plan(Config& cfg, const Args& args) {
  std::string err;
  const Site site = resolve_site(cfg, args, &err);
  if (!err.empty()) {
    std::fprintf(stderr, "%s\n", err.c_str());
    return 1;
  }
  const double start = args.date_arg.empty() ? static_cast<double>(std::time(nullptr))
                                             : parse_date_or_now(args.date_arg);
  const double focal = args.focal > 0 ? args.focal : cfg.num("lens_focal_mm", 6.0);
  const ShootPlan plan =
      make_plan(site, start, args.days > 0 ? args.days : 7, focal, cfg.num("sensor_width_mm", 6.287));
  if (args.json)
    std::printf("%s\n", plan_to_json(plan).dump(2).c_str());
  else
    std::printf("%s", format_plan(plan).c_str());
  return 0;
}

int cmd_align(Config& cfg, const Args& args) {
  if (args.positional.empty()) {
    std::fprintf(stderr, "align wants one or more session directories\n");
    return 2;
  }
  std::string err;
  const Site site = resolve_site(cfg, args, &err);
  if (!err.empty()) {
    std::fprintf(stderr, "%s\n", err.c_str());
    return 1;
  }
  const AlignResult res = align_days(args.positional, site, args.tolerance);
  std::printf("%s\n", res.summary().c_str());
  if (!res.error.empty()) return 1;
  const std::string out_path =
      args.out_path.empty() ? (fs::path(args.positional[0]) / "align.json").string()
                            : args.out_path;
  const std::string payload = res.to_json().dump(2) + "\n";
  if (!write_file_atomic(out_path, payload.data(), payload.size())) {
    std::fprintf(stderr, "could not write %s\n", out_path.c_str());
    return 1;
  }
  std::printf("manifest -> %s\n", out_path.c_str());
  return 0;
}

int cmd_site(Config& cfg, const Args& args) {
  if (!args.positional.empty() && args.positional[0] == "set") {
    if (args.positional.size() < 2) {
      std::fprintf(stderr, "site set wants \"lat,lon\"\n");
      return 2;
    }
    double lat = 0, lon = 0;
    if (!parse_latlon(args.positional[1], &lat, &lon)) {
      std::fprintf(stderr, "could not parse \"%s\" as lat,lon\n", args.positional[1].c_str());
      return 2;
    }
    cfg.set("site_set", Json(true));
    cfg.set("site_lat", Json(lat));
    cfg.set("site_lon", Json(lon));
    cfg.set("site_elev_m", Json(args.elev));
    if (!args.name_arg.empty()) cfg.set("site_name", Json(args.name_arg));
    if (!cfg.save()) {
      std::fprintf(stderr, "could not save %s\n", cfg.path().c_str());
      return 1;
    }
    std::printf("site %s saved\n", format_latlon(lat, lon).c_str());
    return 0;
  }
  if (cfg.boolean("site_set", false)) {
    const Site s = cfg.site();
    std::printf("%s  elev %.0f m%s%s\n", format_latlon(s.lat_deg, s.lon_deg).c_str(), s.elev_m,
                s.name.empty() ? "" : "  ", s.name.c_str());
  } else {
    std::printf("no site configured\n");
  }
  return 0;
}

int cmd_sessions(Config& cfg) {
  const std::string root = expand_user(cfg.str("data_root", "~/birdshot-data"));
  const auto sessions = list_sessions(root);
  if (sessions.empty()) {
    std::printf("no sessions under %s\n", root.c_str());
    return 0;
  }
  for (const auto& dir : sessions) {
    Session s = Session::open(dir);
    const auto index = s.read_index();
    int ok = 0;
    for (const auto& rec : index)
      if (rec.get("verdict").str_or("ok") == "ok") ++ok;
    std::printf("%-28s %5zu frames  %5d ok\n", s.name().c_str(), index.size(), ok);
  }
  return 0;
}

int cmd_doctor(Config& cfg) {
  int problems = 0;
  auto report = [&](bool ok, const char* what, const std::string& detail) {
    std::printf("  %s  %-28s %s\n", ok ? "ok " : "BAD", what, detail.c_str());
    if (!ok) ++problems;
  };

  report(true, "version", std::string(kVersion) + " (" + kCodename + ")");

  const std::string root = expand_user(cfg.str("data_root", "~/birdshot-data"));
  std::error_code ec;
  fs::create_directories(root, ec);
  report(!ec, "data root", root + (ec ? " -- cannot create" : ""));
  const uint64_t free_mb = free_space_mb(ec ? "." : root);
  report(free_mb > cfg.num("min_free_mb", 2048), "free space",
         std::to_string(free_mb) + " MB (floor " +
             std::to_string(static_cast<long long>(cfg.num("min_free_mb", 2048))) + ")");

  const bool cfg_ok = cfg.save();
  report(cfg_ok, "config writable", cfg.path());

  auto backend = make_backend(cfg);
  const Frame f = backend->capture(2000, 1.0);
  report(!f.y.empty(), "backend capture",
         backend->name() + " " + std::to_string(f.y.w) + "x" + std::to_string(f.y.h));

  const std::vector<uint8_t> jpg = encode_jpeg(f.y, 92);
  report(jpg.size() > 4 && jpg[0] == 0xff && jpg[1] == 0xd8, "jpeg encoder",
         std::to_string(jpg.size()) + " bytes for a test frame");

  report(cfg.boolean("site_set", false), "site",
         cfg.boolean("site_set", false)
             ? format_latlon(cfg.num("site_lat", 0), cfg.num("site_lon", 0))
             : "not set -- sun/plan/align need `birdshot site set`");

  std::printf(problems ? "doctor: %d problem(s)\n" : "doctor: healthy\n", problems);
  return problems ? 1 : 0;
}

int run_engine(Config& cfg, Mode mode, const Args& args) {
  auto backend = make_backend(cfg);
  Engine engine(cfg, *backend);
  g_engine = &engine;
  install_signal_handlers();
  engine.set_log([](const std::string& line) { std::printf("%s\n", line.c_str()); });

  EngineOptions opts;
  opts.mode = mode;
  opts.count = args.count > 0 ? args.count : 0;
  opts.interval_s = args.interval;
  opts.verbose = args.verbose;
  const EngineReport rep = engine.run(opts);
  g_engine = nullptr;

  std::printf("%lld frames (%lld saved, %lld gated) in %.1f s -- %.1f fps\n",
              static_cast<long long>(rep.frames), static_cast<long long>(rep.saved),
              static_cast<long long>(rep.rejected), rep.seconds, rep.fps);
  if (mode == Mode::BirdFlight)
    std::printf("%lld take(s)\n", static_cast<long long>(rep.takes));
  if (!rep.clean) std::printf("stopped: %s\n", rep.stop_reason.c_str());
  return rep.clean ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) return usage();
  const std::string cmd = argv[1];

  Args args;
  std::string err;
  if (!parse_args(argc, argv, 2, &args, &err)) {
    std::fprintf(stderr, "%s\n", err.c_str());
    return 2;
  }

  if (cmd == "version" || cmd == "--version") {
    std::printf("birdshot %s (%s)\n", kVersion, kCodename);
    return 0;
  }
  if (cmd == "help" || cmd == "--help" || cmd == "-h") {
    usage();  // asked-for help is a success, unlike the bad-invocation paths
    return 0;
  }
  if (cmd == "selftest") {
    return run_selftest(args.verbose) == 0 ? 0 : 1;
  }

  Config cfg(args.config_path.empty() ? default_config_path() : args.config_path);

  if (cmd == "info") return cmd_info(cfg);
  if (cmd == "sun") return cmd_sun(cfg, args);
  if (cmd == "plan") return cmd_plan(cfg, args);
  if (cmd == "align") return cmd_align(cfg, args);
  if (cmd == "site") return cmd_site(cfg, args);
  if (cmd == "sessions") return cmd_sessions(cfg);
  if (cmd == "doctor") return cmd_doctor(cfg);
  if (cmd == "capture") return run_engine(cfg, Mode::Collect, args);
  if (cmd == "rapid") return run_engine(cfg, Mode::Rapid, args);
  if (cmd == "timelapse") return run_engine(cfg, Mode::Timelapse, args);
  if (cmd == "birdflight") return run_engine(cfg, Mode::BirdFlight, args);

  std::fprintf(stderr, "unknown command '%s'\n\n", cmd.c_str());
  return usage();
}
