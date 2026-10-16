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
	})
}

// endpointTarget maps a relay-side [exit] / [[pool]] block onto a dial.
func endpointTarget(ep config.Endpoint, token string) dialTarget {
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
	}
}

// listenTarget maps an exit-side [[listen]] block (reverse mode) onto a dial.
func listenTarget(spec config.ListenSpec, token string) dialTarget {
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
	}
}
