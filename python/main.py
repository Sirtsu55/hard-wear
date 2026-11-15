import random
import threading
import time

from arduino.app_utils import App, Bridge

from ble_peer import CODE_TO_GAME, GAME_ORDER, GAME_TITLES, BlePeerManager
from games import GameIO, create_game, route_message
from arduino.app_bricks.llm import LargeLanguageModel  # noqa: F401  keeps arduino:llm brick wired
from local_ai import Cancelled, LocalLLM, LocalSTT, clean_for_oled
from recorder import WavRecorder

DEMO_PROMPTS = [
    "Say a fun travel icebreaker to someone standing next to me, and give me one quick travel tip.",
    "Give me a playful compliment on my outfit and suggest a spontaneous mini-quest for the next 10 minutes.",
    "Tell me a weird two-sentence fact about world travel that fits on a tiny screen.",
]
_prompt_idx = 0


def get_demo_prompt() -> str:
    global _prompt_idx
    p = DEMO_PROMPTS[_prompt_idx % len(DEMO_PROMPTS)]
    _prompt_idx += 1
    return p


# ---- AI output & cancellation tracking ----
cancelled_up_to = 0
cancel_lock = threading.Lock()


def is_cancelled(req_id: int) -> bool:
    with cancel_lock:
        return req_id <= cancelled_up_to


def on_cancel_ai(req_id: int):
    global cancelled_up_to
    with cancel_lock:
        if req_id > cancelled_up_to:
            cancelled_up_to = req_id
    print(f"[ai] cancelled up to req_id={req_id}", flush=True)


Bridge.provide("cancel_ai", on_cancel_ai)


def send_to_device(req_id: int, text: str):
    if is_cancelled(req_id):
        print(f"[ai] dropped reply for cancelled req_id={req_id}", flush=True)
        return
    print(f"[ai -> oled #{req_id}] {text}", flush=True)
    Bridge.notify("show_reply", req_id, text)


# ---- Local AI (offline STT + LLM) ----
LOCAL_SYSTEM_PROMPT = (
    "You are Hard Wear, a friendly wearable buddy. Reply in 1-2 short sentences, "
    "under 25 words, plain ASCII, no emojis or markdown."
)
local_stt = LocalSTT()
local_llm = LocalLLM(LOCAL_SYSTEM_PROMPT)


def progress(req_id: int, title: str, body: str):
    """Short OLED progress state; skipped once the request was cancelled."""
    if not is_cancelled(req_id):
        Bridge.notify("show_status", title[:20], body[:60])


def run_local(req_id: int, wav_path, text_prompt) -> str:
    if wav_path:
        progress(req_id, "Transcribing...", "(2xClick: Cancel)")
        heard = local_stt.transcribe(wav_path)
        if is_cancelled(req_id):
            raise Cancelled()
        if not heard.strip():
            return "I didn't catch that. Hold the knob and speak a bit closer."
        print(f"[ai] heard: {heard}", flush=True)
        prompt = heard
    else:
        prompt = text_prompt
    progress(req_id, "EdgeAI Thinking...", "(2xClick: Cancel)")
    return local_llm.ask(prompt, lambda: is_cancelled(req_id))


def handle_ai(req_id: int, wav_path, text_prompt):
    if is_cancelled(req_id):
        return
    try:
        reply = run_local(req_id, wav_path, text_prompt)
    except Cancelled:
        print(f"[ai] local run cancelled req_id={req_id}", flush=True)
        return
    except Exception as e:
        reply = clean_for_oled(f"AI error: {e}")[:100]
    send_to_device(req_id, reply)


def handle_text_ask(req_id: int):
    handle_ai(req_id, None, get_demo_prompt())


def handle_audio_ask(req_id: int, wav_path: str):
    handle_ai(req_id, wav_path, None)


def on_ask_request(req_id: int):
    threading.Thread(target=handle_text_ask, args=(req_id,), daemon=True).start()


Bridge.provide("ask_request", on_ask_request)


# ---- Mini-Games & BLE Proximity Mutual Auto-Challenge State ----
active_game = None
active_game_name = "pong"
active_game_role = "local"
using_demo_bot = False

# Mutual challenge state machine
challenge_peer_id = 0
challenge_peer_name = ""
i_voted_yes = False
peer_voted_yes = False
peer_preferred_game = ""
ble_host_picking = False   # True when both voted Yes and this badge (Host) is picking the game
game_pick_idx = 0          # Index into GAME_ORDER ["pong", "snake", "tictactoe"]
peers_ui_cursor = 0

game_lock = threading.Lock()


def _reset_challenge_state():
    global challenge_peer_id, challenge_peer_name, i_voted_yes, peer_voted_yes, peer_preferred_game, ble_host_picking
    challenge_peer_id = 0
    challenge_peer_name = ""
    i_voted_yes = False
    peer_voted_yes = False
    peer_preferred_game = ""
    ble_host_picking = False


def send_to_peer(msg: dict):
    if using_demo_bot:
        threading.Thread(target=_demo_bot_reply, args=(msg,), daemon=True).start()
    else:
        ble.send_game_dict(msg)


def show_game(text: str):
    print("[game]", text, flush=True)


def draw_board(snapshot: str):
    Bridge.notify("draw_board", snapshot)


def _start(name: str, role: str):
    global active_game, active_game_name, active_game_role
    ble.busy = True
    active_game_name = name if name in GAME_TITLES else "pong"
    active_game_role = role
    active_game = create_game(active_game_name, GameIO(send_to_peer, show_game, draw_board), role)
    active_game.start()


def _demo_bot_reply(msg: dict):
    """Simulated peer for 1-board testing of Tic-Tac-Toe."""
    if msg.get("type") != "move":
        return
    time.sleep(0.5)
    with game_lock:
        if active_game is None or active_game.over or not using_demo_bot:
            return
        if active_game.name == "tictactoe":
            free = [i for i, v in enumerate(active_game.board) if not v]
            if free:
                pick = 4 if 4 in free else random.choice(free)
                route_message(active_game, {"game": "tictactoe", "type": "move", "cell": pick})


def _host_begin(gname: str):
    """Host starts its own game only once the guest acked (or the ack timed out)."""
    Bridge.notify("ble_start_game", gname)
    with game_lock:
        if active_game is not None and not active_game.over:
            active_game.quit(notify_peer=False)
        _start(gname, "host")


def _show_game_picker(is_ble_host: bool):
    global game_pick_idx
    gname = GAME_ORDER[game_pick_idx % len(GAME_ORDER)]
    title = GAME_TITLES[gname]
    hdr = f"1v1 vs {challenge_peer_name}" if is_ble_host else "MINI GAMES (SOLO)"
    Bridge.notify("show_picker", hdr[:20], title, game_pick_idx % len(GAME_ORDER))


def _check_mutual_ready():
    """Called whenever either side votes YES; transitions once BOTH badges voted YES."""
    global ble_host_picking, game_pick_idx
    if not (i_voted_yes and peer_voted_yes and challenge_peer_id):
        return
    if ble_host_picking:
        return  # duplicate vote (same vote arrives over BLE and UDP): don't reset the picker

    pid = challenge_peer_id
    pname = challenge_peer_name
    if pid == 0xB071:
        # Demo bot peer -> local user acts as Host and picks the game!
        ble_host_picking = True
        game_pick_idx = 0
        _show_game_picker(is_ble_host=True)
        return

    if peer_preferred_game in GAME_TITLES and not ble.is_host_vs(pid):
        # Host already sent a specific game code
        ble.busy = True
        Bridge.notify("ble_start_game", peer_preferred_game)
        with game_lock:
            if active_game is not None and not active_game.over:
                active_game.quit(notify_peer=False)
            _start(peer_preferred_game, "guest")
        return

    if ble.is_host_vs(pid):
        # Higher node_id is Host -> let Host pick Pong / Snake / Tic-Tac-Toe!
        ble_host_picking = True
        game_pick_idx = 0
        _show_game_picker(is_ble_host=True)
    else:
        # Lower node_id is Guest -> wait briefly for Host to pick the game
        Bridge.notify(
            "show_status",
            f"MATCH: {pname}"[:20],
            f"{pname} is picking the game... (2xClick: Cancel)",
        )


# ---- BLE Callbacks ----
def _on_ble_peers_changed(count: int):
    print(f"[ble] nearby peers: {count}", flush=True)
    Bridge.notify("ble_peer_count", count)


def _on_auto_challenge(peer_id: int, peer_name: str):
    """Triggered automatically on BOTH badges when they discover each other nearby!"""
    global challenge_peer_id, challenge_peer_name, i_voted_yes, peer_voted_yes, using_demo_bot
    if ble.busy:
        return
    ble.busy = True
    using_demo_bot = False
    challenge_peer_id = peer_id
    challenge_peer_name = peer_name
    i_voted_yes = False
    peer_voted_yes = False
    print(f"[ble] proximity auto-challenge with {peer_name}!", flush=True)
    Bridge.notify("ble_invite", peer_name, "Challenge Peer?")


def _on_peer_voted_yes(peer_id: int, peer_name: str, game_code: int):
    global challenge_peer_id, challenge_peer_name, peer_voted_yes, peer_preferred_game, using_demo_bot
    using_demo_bot = False
    if game_code in CODE_TO_GAME:
        peer_preferred_game = CODE_TO_GAME[game_code]

    # If we weren't already showing the popup for this peer and aren't in a game, pop it up now!
    if challenge_peer_id == 0 and (active_game is None or active_game.over):
        ble.busy = True
        challenge_peer_id = peer_id
        challenge_peer_name = peer_name
        peer_voted_yes = True
        Bridge.notify("ble_invite", peer_name, "Peer Ready! Play?")
        return

    if challenge_peer_id == peer_id:
        peer_voted_yes = True
        _check_mutual_ready()


def _on_peer_declined(peer_id: int, peer_name: str):
    if challenge_peer_id == peer_id:
        _reset_challenge_state()
        ble.busy = False
        Bridge.notify("ble_cancelled", f"{peer_name} said No")


def _on_ble_start_match(peer_id: int, peer_name: str, game_name: str, role: str):
    global using_demo_bot
    with game_lock:
        # Repeated START_MATCH packets for a match already running must not restart it.
        if (
            active_game is not None
            and not active_game.over
            and active_game_name == game_name
            and ble._active_peer_id == peer_id
        ):
            return
    _reset_challenge_state()
    using_demo_bot = False
    ble.busy = True
    Bridge.notify("ble_start_game", game_name)
    with game_lock:
        if active_game is not None and not active_game.over:
            active_game.quit(notify_peer=False)
        _start(game_name, role)


def _on_ble_game_msg(msg: dict):
    with game_lock:
        route_message(active_game, msg)


ble = BlePeerManager(
    on_peers_changed=_on_ble_peers_changed,
    on_auto_challenge=_on_auto_challenge,
    on_peer_voted_yes=_on_peer_voted_yes,
    on_peer_declined=_on_peer_declined,
    on_start_match=_on_ble_start_match,
    on_game_msg=_on_ble_game_msg,
)
ble.start()


# ---- Game Picker & BLE Peers UI Handlers ----
def on_picker_ui(action: str):
    """Handles turning/selecting in either Solo Game Picker or 1v1 BLE Host Game Picker."""
    global game_pick_idx, ble_host_picking, using_demo_bot
    if action == "open_solo":
        _reset_challenge_state()
        ble.busy = True
        game_pick_idx = 0
        _show_game_picker(is_ble_host=False)
    elif action == "next":
        game_pick_idx = (game_pick_idx + 1) % len(GAME_ORDER)
        _show_game_picker(is_ble_host=ble_host_picking)
    elif action == "prev":
        game_pick_idx = (game_pick_idx - 1) % len(GAME_ORDER)
        _show_game_picker(is_ble_host=ble_host_picking)
    elif action == "select":
        gname = GAME_ORDER[game_pick_idx % len(GAME_ORDER)]
        if ble_host_picking and challenge_peer_id:
            pid = challenge_peer_id
            _reset_challenge_state()
            if pid == 0xB071:
                using_demo_bot = (gname == "tictactoe")
                role = "local" if gname in ("pong", "snake") else "host"
            else:
                using_demo_bot = False
                Bridge.notify("show_status", "STARTING...", "Syncing with peer"[:40])
                ble.broadcast_start_match(pid, gname, on_ready=lambda g=gname: _host_begin(g))
                return
            Bridge.notify("ble_start_game", gname)
            with game_lock:
                if active_game is not None and not active_game.over:
                    active_game.quit(notify_peer=False)
                _start(gname, role)
        else:
            # Solo / local match
            using_demo_bot = False
            Bridge.notify("ble_start_game", gname)
            with game_lock:
                if active_game is not None and not active_game.over:
                    active_game.quit(notify_peer=False)
                _start(gname, "local")
    elif action == "cancel":
        if challenge_peer_id:
            ble.vote_no(challenge_peer_id)
        _reset_challenge_state()
        ble.busy = False


Bridge.provide("picker_ui", on_picker_ui)


def _build_peers_entries():
    peers = ble.get_peers()
    entries = []
    for p in peers:
        entries.append({
            "kind": "peer",
            "id": p["id"],
            "name": p["name"],
            "label": f"{p['name']} ({p['rssi']}dB)",
        })
    entries.append({
        "kind": "bot_challenge",
        "id": 0xB071,
        "name": "HW-DEMO",
        "label": "[Sim Auto-Challenge]",
    })
    return entries


def _render_peers_screen():
    global peers_ui_cursor
    entries = _build_peers_entries()
    peers_ui_cursor %= len(entries)
    real_cnt = ble.peer_count()
    me = ble.badge_name
    sel = entries[peers_ui_cursor]
    hdr = f"PEERS({real_cnt}) {me}"
    body = f"> {sel['label']}  (Click:Challenge)"
    Bridge.notify("show_status", hdr[:21], body)


def on_ble_ui(action: str):
    global peers_ui_cursor, challenge_peer_id, challenge_peer_name, i_voted_yes, peer_voted_yes, using_demo_bot
    entries = _build_peers_entries()
    if action == "open":
        peers_ui_cursor = 0
        Bridge.notify("ble_peer_count", ble.peer_count())
        _render_peers_screen()
    elif action == "next":
        peers_ui_cursor = (peers_ui_cursor + 1) % len(entries)
        _render_peers_screen()
    elif action == "prev":
        peers_ui_cursor = (peers_ui_cursor - 1) % len(entries)
        _render_peers_screen()
    elif action == "select":
        sel = entries[peers_ui_cursor % len(entries)]
        if sel["kind"] == "peer":
            ble.clear_cooldown(sel["id"])
            ble.busy = True
            challenge_peer_id = sel["id"]
            challenge_peer_name = sel["name"]
            i_voted_yes = True
            peer_voted_yes = False
            ble.vote_yes(sel["id"])
            Bridge.notify(
                "show_status",
                f"CHALLENGED {sel['name']}"[:21],
                "Waiting for peer to click Yes... (2xClick: Cancel)",
            )
        elif sel["kind"] == "bot_challenge":
            ble.busy = True
            challenge_peer_id = 0xB071
            challenge_peer_name = "HW-DEMO"
            i_voted_yes = False
            peer_voted_yes = True
            using_demo_bot = True
            Bridge.notify("ble_invite", "HW-DEMO", "Challenge Peer?")
    elif action == "accept":
        if challenge_peer_id:
            i_voted_yes = True
            if challenge_peer_id != 0xB071:
                ble.vote_yes(challenge_peer_id)
            if peer_voted_yes:
                _check_mutual_ready()
            else:
                Bridge.notify(
                    "show_status",
                    "YOU SAID YES!",
                    f"Waiting for {challenge_peer_name} to accept... (2xClick: Cancel)",
                )
    elif action == "decline":
        if challenge_peer_id:
            if challenge_peer_id != 0xB071:
                ble.vote_no(challenge_peer_id)
            _reset_challenge_state()
        ble.busy = False


Bridge.provide("ble_ui", on_ble_ui)


def on_game_local(action: str):
    global active_game, using_demo_bot
    with game_lock:
        if action == "quit":
            if active_game is not None and not active_game.over:
                active_game.quit()
            active_game = None
            using_demo_bot = False
            if ble._active_peer_id:
                ble.set_cooldown(ble._active_peer_id)
            _reset_challenge_state()
            ble.busy = False
            ble.advertise_idle()
            return
        if active_game is None:
            return
        if active_game.over:
            if action == "press":
                if active_game_role == "local":
                    _start(active_game_name, "local")
                elif active_game_role == "host" and ble._active_peer_id:
                    gname = active_game_name
                    ble.broadcast_start_match(
                        ble._active_peer_id, gname, on_ready=lambda g=gname: _host_begin(g)
                    )
            return
        active_game.handle_local(action)


Bridge.provide("game_input", on_game_local)


# ---- Mic Recording & Hold-to-Talk local AI ----
recorder = WavRecorder()


def on_audio_start() -> bool:
    recorder.start()
    return True


def on_audio_chunk(index: int, hex_data: str) -> bool:
    recorder.add_chunk(index, hex_data)
    return True


def on_audio_end(total_samples: int) -> bool:
    path = recorder.finish(total_samples)
    print(f"[mic] saved {path} ({total_samples} samples)", flush=True)
    return True


def on_audio_ask_end(total_samples: int, req_id: int) -> bool:
    path = recorder.finish(total_samples)
    if is_cancelled(req_id):
        print(f"[mic] cancelled before local AI (req_id={req_id})", flush=True)
        return True
    secs = total_samples / 8000.0
    print(f"[mic -> ai #{req_id}] saved {path} ({secs:.2f}s, {total_samples} samples)", flush=True)
    threading.Thread(target=handle_audio_ask, args=(req_id, path), daemon=True).start()
    return True


Bridge.provide("audio_start", on_audio_start)
Bridge.provide("audio_chunk", on_audio_chunk)
Bridge.provide("audio_end", on_audio_end)
Bridge.provide("audio_ask_end", on_audio_ask_end)


def on_mcu_log(msg: str):
    print("[mcu]", msg, flush=True)


Bridge.provide("mcu_log", on_mcu_log)


def request_mcu_status():
    time.sleep(1.0)
    Bridge.notify("ping_status")
    Bridge.notify("ble_peer_count", ble.peer_count())


threading.Thread(target=request_mcu_status, daemon=True).start()

App.run()
