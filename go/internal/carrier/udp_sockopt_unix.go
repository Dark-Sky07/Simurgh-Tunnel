//go:build linux || darwin || freebsd || netbsd || openbsd || dragonfly

package carrier

import "syscall"

// getsockoptInt reads a socket option (SO_RCVBUF) so the carrier can advertise
// a window it can actually hold.
func getsockoptInt(fd, opt int) int {
	value, err := syscall.GetsockoptInt(fd, syscall.SOL_SOCKET, opt)
	if err != nil {
		return 0
	}
	return value
}

const soRCVBUF = syscall.SO_RCVBUF
