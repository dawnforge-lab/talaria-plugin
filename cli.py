"""`hermes talaria` pairing and diagnostics commands."""

from __future__ import annotations

import argparse
import http.client
import socket

from . import e2e
from .client import RelayRejected, UnpairedLocally, build_outgoing, claim_pairing, e2e_status, load_state, post_event, record_relay_accepted, record_relay_refusal, send_now, set_redact_content, state_path, unpair
from .relay_urls import check_request_url
from .events import event_id, plugin_hello, push_event


def register_cli(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="talaria_push_action")
    pair = commands.add_parser("pair", help="Pair this Hermes profile with a Talaria device")
    pair.add_argument("code")
    pair.add_argument("--relay-url", default="", help="The relay's address; defaults to this profile's current relay")
    pair.add_argument("--name", default="")
    commands.add_parser("status", help="Show pairing status without revealing credentials")
    test = commands.add_parser("test", help="Send a test notification to the paired device")
    test.add_argument(
        "--corrupt",
        action="store_true",
        help="Send an encrypted test push with a broken ciphertext; the phone must show only generic text",
    )
    commands.add_parser("unpair", help="Revoke this profile's relay credential")
    redact = commands.add_parser(
        "redact",
        help="Redact chat text from pushes; choice labels still transit so cards stay answerable",
    )
    redact.add_argument("mode", choices=["on", "off"])
    parser.set_defaults(func=dispatch)


# What a relay request or a bad saved pairing can raise: printed as one line.
_RELAY_ERRORS = (RuntimeError, OSError, ValueError, LookupError, http.client.HTTPException)


def _printable(value: object) -> str:
    return "".join(c for c in str(value) if c.isprintable())


def dispatch(args: argparse.Namespace) -> int:
    action = getattr(args, "talaria_push_action", None)
    if action == "pair":
        relay_url = str(args.relay_url or (load_state() or {}).get("relay_url") or "")
        if not relay_url:
            print("Pairing needs the relay's address: add --relay-url https://your-relay.example. "
                  "Pairing from Talaria's Settings > Notifications fills it in for you.")
            return 1
        try:
            state = claim_pairing(args.code, relay_url, args.name)
        except _RELAY_ERRORS as error:
            print(f"Pairing failed: {error}")
            if getattr(error, "detail", "") == "gateway_limit_reached":
                print("The relay holds too many pairings for that phone. Turn notifications off and on again in "
                      "Talaria's Settings > Notifications to start fresh, then pair with a new code.")
            if getattr(error, "edge_blocked", False):
                print("That empty 403 came from something in front of the relay blocking this host, so pairing "
                      "can't get through. To get it lifted, ask whoever runs the relay.")
            return 1
        print(f"Paired {state['gateway_name']} with Talaria.")
        try:
            send_now(plugin_hello(), timeout=4.0)
        except Exception as error:
            # Pairing succeeded; a failed announcement only means the app's
            # compatibility view fills in on the first real event instead.
            print(f"Warning: could not announce plugin version: {error}")
        return 0
    if action == "status":
        state = load_state()
        if not state:
            print("This Hermes profile is not paired with Talaria.")
            return 1
        print(f"Paired: {_printable(state.get('gateway_name') or socket.gethostname())}")
        relay_url = str(state.get('relay_url') or "")
        print(f"Relay: {_printable(relay_url) or '(none)'}")
        try:
            check_request_url(f"{relay_url.rstrip('/')}/v1/events")
        except ValueError:
            print("Warning: this is not a usable https:// relay URL, so notifications can't be sent. "
                  "Pair again from Talaria, or run `hermes talaria unpair`.")
        if state.get("relay_rejected_at"):
            print(f"Warning: the relay refused this pairing's credential "
                  f"(since {_printable(state['relay_rejected_at'])}), so notifications aren't arriving. "
                  "Talaria removed this gateway or the relay lost its data: pair again from Talaria.")
        if state.get("relay_blocked_at"):
            print(f"Warning: something in front of the relay (a firewall or rate limiter) has been answering this host "
                  f"with an empty 403 since {_printable(state['relay_blocked_at'])}, so notifications, calls and "
                  "live turn progress aren't arriving. Pairing again won't help: it meets the same block. "
                  "To get it lifted, ask whoever runs the relay.")
        print(f"Redact content: {'on' if state.get('redact_content') else 'off'}")
        encryption = e2e_status(state)
        if encryption["enabled"]:
            print(f"End-to-end encryption: on (key {encryption['kid'][:8]}…)")
        elif not encryption["crypto"]:
            print("End-to-end encryption: off (this host is missing the cryptography package)")
        else:
            print("End-to-end encryption: off (open Talaria > Settings > Notifications to turn it on)")
        print(f"State: {state_path()}")
        return 0
    if action == "test":
        event = push_event(
            "response.ready",
            identifier=event_id("test"),
            profile=_profile_name(),
            title="Talaria",
            body="Push notifications are connected.",
        )
        if getattr(args, "corrupt", False):
            state = load_state()
            if not state:
                print("This Hermes profile is not paired with Talaria.")
                return 1
            outgoing = build_outgoing(event, state)
            if "e2e" not in outgoing:
                print("End-to-end encryption is off for this profile; nothing to corrupt.")
                return 1
            outgoing["e2e"]["ct"] = _flipped(outgoing["e2e"]["ct"])
            try:
                post_event(outgoing)
            except _RELAY_ERRORS as error:
                if isinstance(error, RelayRejected) and error.status in (401, 403):
                    record_relay_refusal(state, error)
                print(f"The Talaria relay did not accept the test notification: {error}")
                return 1
            record_relay_accepted(state)
            print("Broken encrypted test notification accepted by the Talaria relay. The phone should show only generic text.")
            return 0
        try:
            send_now(event)
        except _RELAY_ERRORS as error:
            print(f"The Talaria relay did not accept the test notification: {error}")
            if getattr(error, "edge_blocked", False):
                print("That empty 403 came from something in front of the relay blocking this host, not from the "
                      "relay, so pairing again won't help. To get it lifted, ask whoever runs the relay.")
            return 1
        print("Test notification accepted by the Talaria relay.")
        return 0
    if action == "redact":
        if not set_redact_content(args.mode == "on"):
            print("This Hermes profile is not paired with Talaria.")
            return 1
        print(
            "Notification content is redacted; approval and clarify cards use generic text."
            if args.mode == "on"
            else "Notification content is no longer redacted."
        )
        return 0
    if action == "unpair":
        try:
            revoked = unpair()
        except UnpairedLocally as error:
            print(error)
            return 0
        except _RELAY_ERRORS as error:
            print(f"Unpairing failed: {error}")
            return 1
        print("Talaria pairing revoked." if revoked else "This Hermes profile was not paired.")
        return 0
    print("Usage: hermes talaria {pair|status|test|redact|unpair}")
    return 2


def _flipped(ciphertext: str) -> str:
    raw = bytearray(e2e.unb64u(ciphertext))
    raw[0] ^= 0x01
    return e2e.b64u(bytes(raw))


def _profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name()
    except Exception:
        return "default"
