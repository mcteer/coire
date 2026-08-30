// Static health probe for shell-less images.
//
// The Chainguard nginx and upstream collector images have no shell and no curl, so their
// container healthchecks need a self-contained binary. Built with CGO_ENABLED=0 and copied
// into the runtime stage (research R2).
//
// Two modes:
//
//	healthcheck <url>              GET the URL; exit 0 on 2xx, 1 otherwise
//	healthcheck --tcp host:port    TCP connect; exit 0 on success, 1 otherwise
//
// The --tcp mode exists because a plain HTTP probe cannot distinguish "no route to this host"
// from "this host does not speak HTTP" — Postgres would fail an HTTP probe whether or not the
// network policy blocks it, which would make the topology assertion in test_restart_isolation
// vacuous.
package main

import (
	"fmt"
	"net"
	"net/http"
	"os"
	"time"
)

const timeout = 2 * time.Second

func main() {
	args := os.Args[1:]
	if len(args) == 0 {
		fmt.Fprintln(os.Stderr, "usage: healthcheck <url> | healthcheck --tcp host:port")
		os.Exit(2)
	}

	if args[0] == "--tcp" {
		if len(args) < 2 {
			fmt.Fprintln(os.Stderr, "usage: healthcheck --tcp host:port")
			os.Exit(2)
		}
		conn, err := net.DialTimeout("tcp", args[1], timeout)
		if err != nil {
			fmt.Fprintf(os.Stderr, "tcp %s: %v\n", args[1], err)
			os.Exit(1)
		}
		_ = conn.Close()
		return
	}

	client := &http.Client{Timeout: timeout}
	resp, err := client.Get(args[0])
	if err != nil {
		fmt.Fprintf(os.Stderr, "get %s: %v\n", args[0], err)
		os.Exit(1)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		fmt.Fprintf(os.Stderr, "get %s: HTTP %d\n", args[0], resp.StatusCode)
		os.Exit(1)
	}
}
