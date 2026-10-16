package node

import (
	"time"

	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/carrier"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/config"
	"github.com/Dark-Sky07/Simurgh-Tunnel/go/internal/mux"
)

// dialTarget is everything a tunnel dial needs, whichever role does the
// dialling (the relay in direct mode, the exit in reverse mode).  Having one
// struct keeps the transport choice -- including the udp carrier, which takes
// a different constructor -- in exactly one place.
type dialTarget struct {
	Carrier     string
	Address     string
	Port        int
	Domain      string
	Path        string
	Fingerprint string
	Insecure    bool
	Padding     bool
	Split       int
	MTU         int
	FEC         string
	Window      int
	Token       string
	NoDelay     bool
	Keepalive   int
}

// dial opens one tunnel transport.
func (d dialTarget) dial() (mux.Channel, error) {
	if carrier.IsUDPName(d.Carrier) {
		return carrier.DialUDP(carrier.ClientOptions{
			Carrier:     d.Carrier,
			Address:     d.Address,
			Port:        d.Port,
			Domain:      d.Domain,
			Path:        d.Path,
			Fingerprint: d.Fingerprint,
			Insecure:    d.Insecure,
			Padding:     d.Padding,
			Split:       d.Split,
			Token:       d.Token,
			Timeout:     10 * time.Second,
			NoDelay:     d.NoDelay,
			Keepalive:   d.Keepalive,
		}, carrier.UDPOptions{MTU: d.MTU, FEC: d.FEC, Window: d.Window})
	}
	return carrier.Dial(carrier.ClientOptions{
		Carrier:     d.Carrier,
		Address:     d.Address,
		Port:        d.Port,
		Domain:      d.Domain,
		Path:        d.Path,
		Fingerprint: d.Fingerprint,
		Insecure:    d.Insecure,
		Padding:     d.Padding,
		Split:       d.Split,
		Token:       d.Token,
		Timeout:     10 * time.Second,
		NoDelay:     d.NoDelay,
		Keepalive:   d.Keepalive,
	})
}

// endpointTarget maps a relay-side [exit] / [[pool]] block onto a dial.
func endpointTarget(ep config.Endpoint, token string, fallback ...config.Endpoint) dialTarget {
	// the file-level values arrive as the fallback endpoint's own fields, so a
	// `[exit]`/`[[pool]]` block can override tcp_nodelay/tcp_keepalive exactly
	// like it can in the Python engine
	noDelay, keepalive := true, 0
	if len(fallback) > 0 {
		// the file's values, never the [exit] block's own overrides
		noDelay, keepalive = fallback[0].FileNoDelayValue(), fallback[0].FileKeepaliveValue()
	}
	if ep.TCPNoDelay != nil {
		noDelay = *ep.TCPNoDelay
	}
	if ep.TCPKeepalive != nil {
		keepalive = *ep.TCPKeepalive
	}
	return dialTarget{
		Carrier:     ep.Carrier,
		Address:     ep.Address,
		Port:        ep.Port,
		Domain:      ep.Domain,
		Path:        ep.Path,
		Fingerprint: ep.Fingerprint,
		Insecure:    ep.InsecureSkipVerify,
		Padding:     ep.Padding,
		Split:       ep.Split,
		MTU:         ep.MTU,
		FEC:         ep.FEC,
		Window:      ep.Window,
		Token:       token,
		NoDelay:     noDelay,
		Keepalive:   keepalive,
	}
}

// listenTarget maps an exit-side [[listen]] block (reverse mode) onto a dial.
func listenTarget(spec config.ListenSpec, token string, noDelay bool, keepalive int) dialTarget {
	return dialTarget{
		Carrier:     spec.Carrier,
		Address:     spec.Dial,
		Port:        spec.Port,
		Domain:      spec.Path, // the SNI stays the address, as the Python engine does
		Fingerprint: spec.Fingerprint,
		Insecure:    spec.InsecureSkipVerify,
		Padding:     spec.Padding,
		Split:       spec.Split,
		MTU:         spec.MTU,
		FEC:         spec.FEC,
		Window:      spec.Window,
		Token:       token,
		NoDelay:     noDelay,
		Keepalive:   keepalive,
	}
}
