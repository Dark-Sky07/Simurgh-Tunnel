package node

import (
	"testing"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/config"
)

// The socket-option keys (`tcp_nodelay`, `tcp_keepalive`) are part of the
// shared config, and `simurgh tune` writes them, so the Go side has to resolve
// them exactly like the Python engine's relay._net_opts: the endpoint's own
// value wins, the file's value applies otherwise.

func TestEndpointTargetResolvesTheSocketOptions(t *testing.T) {
	fileNoDelay, fileKeepalive := true, 15
	relay := config.Relay{TCPNoDelay: fileNoDelay, TCPKeepalive: fileKeepalive}
	relay.Exit.FileNoDelay, relay.Exit.FileKeepalive = fileNoDelay, fileKeepalive

	inherits := endpointTarget(relay.Exit, "tok", relay.Exit)
	if !inherits.NoDelay || inherits.Keepalive != 15 {
		t.Fatalf("an endpoint without its own keys must inherit the file: nodelay=%v keepalive=%d",
			inherits.NoDelay, inherits.Keepalive)
	}

	off := false
	nilKeep := 0
	override := config.Endpoint{
		Address: "127.0.0.1", Port: 8443,
		TCPNoDelay: &off, TCPKeepalive: &nilKeep,
		FileNoDelay: fileNoDelay, FileKeepalive: fileKeepalive,
	}
	got := endpointTarget(override, "tok", relay.Exit)
	if got.NoDelay || got.Keepalive != 0 {
		t.Fatalf("an endpoint's own keys must win: nodelay=%v keepalive=%d",
			got.NoDelay, got.Keepalive)
	}
}

func TestASiblingEndpointInheritsTheFileNotTheExitBlock(t *testing.T) {
	// `[exit] tcp_keepalive = 30` belongs to that endpoint only; a [[pool]]
	// entry must still see the file's own value.
	fileKeep := 5
	own := 30
	exit := config.Endpoint{
		Address: "127.0.0.1", Port: 8443,
		TCPKeepalive: &own, FileNoDelay: true, FileKeepalive: fileKeep,
	}
	pool := config.Endpoint{
		Address: "127.0.0.2", Port: 8443,
		FileNoDelay: true, FileKeepalive: fileKeep,
	}
	if got := endpointTarget(pool, "tok", exit); got.Keepalive != fileKeep {
		t.Fatalf("a pool entry inherited the [exit] block's tcp_keepalive: %d", got.Keepalive)
	}
	if got := endpointTarget(exit, "tok", exit); got.Keepalive != own {
		t.Fatalf("the [exit] block's own tcp_keepalive was dropped: %d", got.Keepalive)
	}
}

func TestListenTargetCarriesTheExitSocketOptions(t *testing.T) {
	spec := config.ListenSpec{
		Carrier: "raw", Dial: "relay.example", Port: 8443, Path: "/ws",
		Fingerprint: "aa", InsecureSkipVerify: true, Padding: true,
		MTU: 1400, FEC: "off", Window: 0, Enabled: true,
	}
	target := listenTarget(spec, "tok", false, 30)
	if target.NoDelay || target.Keepalive != 30 {
		t.Fatalf("listenTarget dropped the socket options: nodelay=%v keepalive=%d",
			target.NoDelay, target.Keepalive)
	}
	if target.Address != "relay.example" || target.Port != 8443 || target.Token != "tok" {
		t.Fatalf("listenTarget lost the spec: %+v", target)
	}
	if target.Fingerprint != "aa" || !target.Insecure || !target.Padding {
		t.Fatalf("listenTarget lost the tls settings: %+v", target)
	}
}
