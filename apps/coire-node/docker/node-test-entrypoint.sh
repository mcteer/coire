#!/bin/sh
# Keep the agent running, the way launchd's KeepAlive does on a Studio.
#
# The restart test kills the agent process and expects the engines it started to survive and
# be re-adopted (spec FR-015). This loop is the KeepAlive stand-in: it restarts the agent
# without touching anything else in the container, so the test exercises adoption rather than
# a whole-container restart, which would prove nothing.
set -eu

while true; do
  python -m coire_node || echo "coire-node exited $? — restarting in 1s" >&2
  sleep 1
done
