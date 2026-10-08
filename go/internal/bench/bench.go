// Package bench measures the engine through a simulated long path: a delay
// proxy sits between the relay and the exit, exactly like tools/latency_bench.py
// does for the Python engine, so the two numbers are comparable.
package bench

import (
	"flag"
	"fmt"
	"io"
	"log/slog"
	"math/rand"
	"net"
	"os"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/config"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/node"
)

type options struct {
	delayMS     float64 // added latency per megabyte per direction
	megabytes   int
	carrier     string
	window      int
	maxWindow   int
	connections int
	streams     int
	rateMBS     float64 // per tunnel connection cap
	quiet       bool
	verbose     bool
}

// Run executes the benchmark and returns a process exit code.
func Run(args []string) int {
	fs := flag.NewFlagSet("bench", flag.ExitOnError)
	opt := options{}
	fs.Float64Var(&opt.delayMS, "delay", 60, "one-way propagation delay in milliseconds")
	fs.IntVar(&opt.megabytes, "mb", 40, "megabytes to transfer")
	fs.StringVar(&opt.carrier, "carrier", "plain", "plain|tls")
	fs.IntVar(&opt.window, "window", config.DefaultStreamWindow, "starting stream window (bytes)")
	fs.IntVar(&opt.maxWindow, "max-window", config.DefaultMaxStreamWindow, "window ceiling (bytes)")
	fs.IntVar(&opt.connections, "connections", 1, "tunnel connections in the pool")
	fs.IntVar(&opt.streams, "streams", 1, "parallel user connections")
	fs.Float64Var(&opt.rateMBS, "rate", 0, "cap each tunnel connection at N MB/s (0 = none)")
	fs.BoolVar(&opt.quiet, "quiet", false, "only print the result line")
	fs.BoolVar(&opt.verbose, "verbose", false, "debug logging")
	_ = fs.Parse(args)
	level := slog.LevelError
	if opt.verbose {
		level = slog.LevelDebug
	}
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: level})))

	home, err := os.MkdirTemp("", "simurgh-bench-")
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 1
	}
	defer os.RemoveAll(home)

	// 1. the sink: an echo server that answers with the payload it is asked for
	sinkPort, stopSink, err := startSink()
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 1
	}
	defer stopSink()

	// 2. the exit
	exitPort := freePort()
	exitCfg := &config.Exit{
		Token: "bench-token", Name: "exit", CertAuto: true,
		StreamWindow: opt.window, MaxStreamWindow: opt.maxWindow,
		Chunk: 64 << 10, Keepalive: 25, Connections: max(1, opt.connections),
		Listen: []config.ListenSpec{{Carrier: opt.carrier, Host: "127.0.0.1", Port: exitPort,
			Path: "/ws", Fallback: "decoy", Padding: true, Enabled: true}},
	}
	exit := node.NewExit(exitCfg, home)
	if err := exit.Start(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 1
	}
	defer exit.Stop()

	// 3. the delay proxy in front of the exit (the long path)
	proxyPort := freePort()
	proxy := &delayProxy{listen: proxyPort, target: exitPort,
		delay: time.Duration(opt.delayMS * float64(time.Millisecond)), rate: opt.rateMBS * 1e6}
	proxy.start()
	defer proxy.stop()

	// 4. the relay, dialling the exit through the proxy
	relayPort := freePort()
	relayCfg := &config.Relay{
		Token: "bench-token", Name: "relay", Dial: "relay",
		StreamWindow: opt.window, MaxStreamWindow: opt.maxWindow,
		Chunk: 64 << 10, Keepalive: 25, Connections: max(1, opt.connections),
		Exit: config.Endpoint{Carrier: opt.carrier, Address: "127.0.0.1", Port: proxyPort,
			Path: "/ws", InsecureSkipVerify: true, Enabled: true},
		Mappings: []config.Mapping{{Name: "sink", Listen: relayPort, ListenHost: "127.0.0.1",
			TargetHost: "127.0.0.1", TargetPort: sinkPort, Enabled: true}},
	}
	relay := node.NewRelay(relayCfg, home)
	if err := relay.Start(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		return 1
	}
	defer relay.Stop()

	want := max(1, opt.connections)
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		if relay.Status()["connections"].(int) >= want {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if got := relay.Status()["connections"].(int); got < want {
		fmt.Fprintf(os.Stderr, "tunnel did not come up (%d/%d connections)\n", got, want)
		return 1
	}

	total := int64(opt.megabytes) << 20
	streams := max(1, opt.streams)
	label := fmt.Sprintf("carrier=%s delay=%.0fms window=%dKiB max=%dKiB conns=%d%s streams=%d",
		opt.carrier, opt.delayMS, opt.window>>10, opt.maxWindow>>10, opt.connections,
		rateLabel(opt.rateMBS), streams)

	// a stall in the data plane is best debugged with every goroutine stack on
	// screen, so the bench always keeps a watchdog running
	watchdog := time.AfterFunc(25*time.Second, func() {
		buf := make([]byte, 1<<20)
		n := runtime.Stack(buf, true)
		fmt.Fprintf(os.Stderr, "bench is stalled, goroutine dump:\n%s\n", buf[:n])
		os.Exit(3)
	})
	defer watchdog.Stop()

	// sample how the pool spreads the streams while it is really busy
	var spreadMu sync.Mutex
	spreadSample := []int(nil)
	spreadStop := make(chan struct{})
	go func() {
		for {
			select {
			case <-spreadStop:
				return
			case <-time.After(250 * time.Millisecond):
				sample := make([]int, 0, 4)
				for _, m := range relay.Pool() {
					sample = append(sample, m.StreamCount())
				}
				if len(sample) > 1 {
					spreadMu.Lock()
					spreadSample = sample
					spreadMu.Unlock()
				}
			}
		}
	}()

	started := time.Now()
	var wg sync.WaitGroup
	results := make([]float64, streams)
	for i := 0; i < streams; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			results[idx] = oneStream(relayPort, total/int64(streams))
		}(i)
	}
	wg.Wait()
	took := time.Since(started).Seconds()

	sum := 0.0
	for _, r := range results {
		sum += r
	}
	sort.Float64s(results)
	fmt.Printf("%s: aggregate %.1f MB/s (%.0f Mbit/s)\n", label,
		sum/took/1e6, sum*8/took/1e6)
	if streams > 1 {
		fmt.Printf("   per stream %.1f-%.1f MB/s in %.1fs\n", results[0]/took/1e6, results[len(results)-1]/took/1e6, took)
	}
	close(spreadStop)
	spreadMu.Lock()
	spread := spreadSample
	spreadMu.Unlock()
	if len(spread) > 1 {
		fmt.Printf("   streams per tunnel connection: %v\n", spread)
	}
	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)
	fmt.Printf("   heap: %.1f MB live, %.1f MB reserved; process peak RSS: %.1f MB; goroutines %d\n",
		float64(mem.HeapAlloc)/1e6, float64(mem.Sys)/1e6, peakRSSMB(), runtime.NumGoroutine())
	return 0
}

// peakRSSMB reads the process high water mark (Linux): what the kernel really
// handed to this process, which is the number that matters on a small VPS.
func peakRSSMB() float64 {
	raw, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return 0
	}
	for _, line := range strings.Split(string(raw), "\n") {
		if strings.HasPrefix(line, "VmHWM:") {
			fields := strings.Fields(line)
			if len(fields) >= 2 {
				if kb, err := strconv.ParseFloat(fields[1], 64); err == nil {
					return kb / 1024
				}
			}
		}
	}
	return 0
}

func rateLabel(rate float64) string {
	if rate == 0 {
		return ""
	}
	return fmt.Sprintf(" cap=%.0fMB/s/conn", rate)
}

// oneStream pulls n bytes from the echo target through the tunnel.
func oneStream(port int, n int64) float64 {
	conn, err := net.Dial("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		return 0
	}
	defer conn.Close()
	// The sink echoes what it receives, so we ask for n bytes and count what
	// comes back; a short request line keeps the sink's logic trivial.
	req := fmt.Sprintf("GET %d\n", n)
	if _, err := conn.Write([]byte(req)); err != nil {
		return 0
	}
	got := int64(0)
	buf := make([]byte, 1<<20)
	for got < n {
		conn.SetReadDeadline(time.Now().Add(60 * time.Second))
		m, err := conn.Read(buf)
		if m > 0 {
			got += int64(m)
		}
		if err != nil {
			break
		}
	}
	return float64(got)
}

// ---------------------------------------------------------------------------
// delay proxy: every byte is forwarded after a delay, optionally rate limited

type delayProxy struct {
	listen int
	target int
	delay  time.Duration // propagation delay for every chunk (one way)
	rate   float64       // bytes per second per connection (0 = unlimited)
	ln     net.Listener
	conns  []net.Conn
	mu     sync.Mutex
}

func (p *delayProxy) start() {
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", p.listen))
	if err != nil {
		panic(err)
	}
	p.ln = ln
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			p.mu.Lock()
			p.conns = append(p.conns, conn)
			p.mu.Unlock()
			go p.handle(conn)
		}
	}()
}

func (p *delayProxy) stop() {
	if p.ln != nil {
		p.ln.Close()
	}
	p.mu.Lock()
	for _, c := range p.conns {
		c.Close()
	}
	p.mu.Unlock()
}

func (p *delayProxy) handle(conn net.Conn) {
	up, err := net.Dial("tcp", fmt.Sprintf("127.0.0.1:%d", p.target))
	if err != nil {
		conn.Close()
		return
	}
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		p.pump(conn, up)
	}()
	go func() {
		defer wg.Done()
		p.pump(up, conn)
	}()
	wg.Wait()
	conn.Close()
	up.Close()
}

type chunk struct {
	due  time.Time
	data []byte
}

// pump forwards one direction of a proxied connection. Reading and writing are
// decoupled through a queue, so the proxy itself never throttles the flow: it
// only adds the propagation delay of the emulated path.
func (p *delayProxy) pump(from, to net.Conn) {
	queue := make(chan chunk, 512)
	senderDone := make(chan struct{})
	go func() {
		defer close(senderDone)
		nextAllowed := time.Now()
		for item := range queue {
			if wait := time.Until(item.due); wait > 0 {
				time.Sleep(wait)
			}
			if p.rate > 0 {
				now := time.Now()
				if nextAllowed.Before(now) {
					nextAllowed = now
				}
				nextAllowed = nextAllowed.Add(time.Duration(float64(len(item.data)) / p.rate * float64(time.Second)))
				if wait := time.Until(nextAllowed); wait > 0 {
					time.Sleep(wait)
				}
			}
			if _, err := to.Write(item.data); err != nil {
				return
			}
		}
	}()

	buf := make([]byte, 64<<10)
	for {
		n, err := from.Read(buf)
		if n > 0 {
			data := make([]byte, n)
			copy(data, buf[:n])
			select {
			case queue <- chunk{due: time.Now().Add(p.delay), data: data}:
			case <-senderDone:
				return
			}
		}
		if err != nil {
			if err != io.EOF {
				slog.Debug("proxy", "err", err)
			}
			break
		}
	}
	close(queue)
	<-senderDone
}

// ---------------------------------------------------------------------------
// helpers

func startSink() (int, func(), error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, nil, err
	}
	var wg sync.WaitGroup
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			wg.Add(1)
			go func() {
				defer wg.Done()
				sinkHandler(conn)
			}()
		}
	}()
	return ln.Addr().(*net.TCPAddr).Port, func() { ln.Close(); wg.Wait() }, nil
}

// sinkHandler reads "GET <bytes>\n" then streams that many bytes of data.
func sinkHandler(conn net.Conn) {
	defer conn.Close()
	buf := make([]byte, 64)
	n, err := conn.Read(buf)
	if err != nil || n < 5 {
		return
	}
	var want int64
	if _, err := fmt.Sscanf(string(buf[:n]), "GET %d", &want); err != nil {
		return
	}
	block := make([]byte, 1<<20)
	rand.Read(block)
	var sent int64
	for sent < want {
		chunk := int64(len(block))
		if want-sent < chunk {
			chunk = want - sent
		}
		if _, err := conn.Write(block[:chunk]); err != nil {
			return
		}
		sent += chunk
	}
}

var portMu sync.Mutex

func freePort() int {
	portMu.Lock()
	defer portMu.Unlock()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

var _ = atomic.Int64{}
