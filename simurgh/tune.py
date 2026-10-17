"""Tuning profiles: one command that sets the knobs that decide behaviour.

Every value here is a key the config already has and **both** engines already
read, so a profile is a shorthand, not a new mechanism:

    simurgh tune gaming     # lowest latency under load
    simurgh tune speed      # highest aggregate throughput
    simurgh tune balanced   # the shipped defaults, back again

The three profiles exist because the interesting choice is a trade-off, not a
setting: a smaller `stream_window` keeps less data in flight (shorter queues,
steadier latency, less memory) and caps a single flow on a long path; a bigger
one does the opposite.  `chunk` is the same trade in frame-sized pieces, and
`connections` is how many TCP flows carry the tunnel -- more of them add
throughput and loss tolerance on a long path, at the cost of a little jitter.

`simurgh tune speed` also raises `connections` to 4 on the relay, which is the
one change that needs both ends reachable: the relay opens them, the exit
accepts them.  Nothing here touches the carrier, the token or the mappings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: shared by both files: the data path of a stream
STREAM_KEYS = ("stream_window", "max_stream_window", "chunk")
#: socket-level behaviour of every connection the node makes
SOCKET_KEYS = ("tcp_nodelay", "tcp_keepalive")


@dataclass(frozen=True)
class Profile:
    name: str
    title: str
    summary: str
    relay: dict = field(default_factory=dict)
    exit: dict = field(default_factory=dict)

    def keys(self, role: str) -> dict:
        return dict(self.relay if role == "relay" else self.exit)


PROFILES: dict[str, Profile] = {
    "gaming": Profile(
        name="gaming",
        title="lowest latency, steadiest under load",
        summary=("small windows and frames so a busy link adds as little queueing "
                 "as possible; the right choice when a human is waiting (games, "
                 "calls, SSH) and throughput is secondary"),
        relay={"keepalive": 10, "stream_window": 131072,
               "max_stream_window": 2097152, "chunk": 16384,
               "connections": 1, "tcp_nodelay": True, "tcp_keepalive": 15},
        exit={"stream_window": 131072, "max_stream_window": 2097152,
              "chunk": 16384, "tcp_nodelay": True, "tcp_keepalive": 15},
    ),
    "balanced": Profile(
        name="balanced",
        title="the shipped defaults",
        summary=("what `simurgh init` writes and what every measurement in the "
                 "README was taken with: 256 KiB windows that grow to 8 MiB, "
                 "64 KiB frames, one tunnel connection"),
        relay={"keepalive": 25, "stream_window": 262144,
               "max_stream_window": 8388608, "chunk": 65536,
               "connections": 1, "tcp_nodelay": True, "tcp_keepalive": 0},
        exit={"stream_window": 262144, "max_stream_window": 8388608,
              "chunk": 65536, "tcp_nodelay": True, "tcp_keepalive": 0},
    ),
    "speed": Profile(
        name="speed",
        title="highest aggregate throughput",
        summary=("bigger starting windows, a 32 MiB ceiling and four tunnel "
                 "connections: for many heavy users on a fast, clean path, and "
                 "the profile that parks the most memory per busy stream"),
        relay={"keepalive": 25, "stream_window": 524288,
               "max_stream_window": 33554432, "chunk": 65536,
               "connections": 4, "tcp_nodelay": True, "tcp_keepalive": 0},
        exit={"stream_window": 524288, "max_stream_window": 33554432,
              "chunk": 65536, "tcp_nodelay": True, "tcp_keepalive": 0},
    ),
}


def names() -> list[str]:
    return list(PROFILES)


def get(name: str) -> Profile:
    """Look a profile up, with a message an operator can act on."""
    key = str(name or "").strip().lower()
    if key not in PROFILES:
        raise KeyError(f"unknown profile '{name}'; try one of: "
                       f"{', '.join(PROFILES)}")
    return PROFILES[key]


def apply(cfg, role: str, name: str) -> list[dict]:
    """Set the profile's keys on a loaded config; return what really changed.

    A key already holding the profile's value is not reported (and, when the
    caller only writes on changes, does not cause a rewrite).
    """
    profile = get(name)
    changes: list[dict] = []
    for key, value in profile.keys(role).items():
        old = getattr(cfg, key, None)
        if old == value:
            continue
        setattr(cfg, key, value)
        changes.append({"key": key, "old": old, "new": value})
    return changes


def describe(name: str | None = None) -> list[dict]:
    """Rows for `simurgh tune --show` (one per profile, or just one)."""
    wanted = [get(name)] if name else [PROFILES[key] for key in PROFILES]
    rows = []
    for profile in wanted:
        rows.append({
            "name": profile.name,
            "title": profile.title,
            "summary": profile.summary,
            "relay": dict(profile.relay),
            "exit": dict(profile.exit),
        })
    return rows
