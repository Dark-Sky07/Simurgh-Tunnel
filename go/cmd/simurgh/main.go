// Command simurgh is the Go engine: a transparent Iran <-> abroad tunnel with
// the same wire protocol as the Python engine, but running every core of the
// machine and one goroutine per user connection.
package main

import (
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"runtime"
	"syscall"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/bench"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/carrier"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/config"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/node"
)

const version = "2.1.0-go"

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "relay":
		os.Exit(run("relay", os.Args[2:]))
	case "exit":
		os.Exit(run("exit", os.Args[2:]))
	case "bench":
		os.Exit(bench.Run(os.Args[2:]))
	case "gencert":
		os.Exit(gencert(os.Args[2:]))
	case "version", "--version", "-v":
		fmt.Printf("simurgh (go engine) %s — %s/%s, %d CPUs\n", version, runtime.GOOS, runtime.GOARCH, runtime.NumCPU())
	case "help", "--help", "-h":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n", os.Args[1])
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Print(`simurgh (go engine) — the fast data plane

  simurgh relay [--home DIR] [--verbose]     run the Iranian side
  simurgh exit  [--home DIR] [--verbose]     run the foreign side
  simurgh bench [options]                    in-process throughput benchmark
  simurgh gencert [--home DIR] [--name CN]   create the TLS certificate
  simurgh version

Config: <home>/relay.toml or <home>/exit.toml (same files as the Python engine)
`)
}

func setupLog(verbose bool) {
	level := slog.LevelInfo
	if verbose {
		level = slog.LevelDebug
	}
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: level})))
}

func run(role string, args []string) int {
	fs := flag.NewFlagSet(role, flag.ExitOnError)
	home := fs.String("home", defaultHome(), "directory with the config and certificate")
	verbose := fs.Bool("verbose", false, "debug logging")
	_ = fs.Parse(args)
	setupLog(*verbose)

	slog.Info("simurgh", "engine", "go "+version, "role", role, "cpus", runtime.NumCPU(), "home", *home)

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)

	if role == "relay" {
		cfg, err := config.LoadRelay(*home + "/relay.toml")
		if err != nil {
			slog.Error("cannot load the relay config", "err", err)
			return 1
		}
		relay := node.NewRelay(cfg, *home)
		if err := relay.Start(); err != nil {
			slog.Error("cannot start", "err", err)
			return 1
		}
		<-stop
		slog.Info("stopping")
		relay.Stop()
		return 0
	}

	cfg, err := config.LoadExit(*home + "/exit.toml")
	if err != nil {
		slog.Error("cannot load the exit config", "err", err)
		return 1
	}
	exit := node.NewExit(cfg, *home)
	if err := exit.Start(); err != nil {
		slog.Error("cannot start", "err", err)
		return 1
	}
	<-stop
	slog.Info("stopping")
	exit.Stop()
	return 0
}

func gencert(args []string) int {
	fs := flag.NewFlagSet("gencert", flag.ExitOnError)
	home := fs.String("home", defaultHome(), "directory for the certificate")
	name := fs.String("name", "simurgh.local", "common name / SNI")
	_ = fs.Parse(args)
	cert, err := carrier.EnsureCert(*home+"/cert", *name)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 1
	}
	fp, err := carrier.Fingerprint(cert)
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 1
	}
	fmt.Println("certificate:", *home+"/cert/cert.pem")
	fmt.Println("fingerprint:", fp)
	return 0
}

func defaultHome() string {
	if env := os.Getenv("SIMURGH_HOME"); env != "" {
		return env
	}
	return "/etc/simurgh"
}
