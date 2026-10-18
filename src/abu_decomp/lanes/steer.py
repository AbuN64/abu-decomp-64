"""Steer a running Codex lane: append input to its active turn through the lane's own app-server socket.
usage: steer.py LANE_DIR SOCKET FILE  (FILE holds the instruction text)
Reads the thread id from LANE_DIR/thread.id and the active turn id from LANE_DIR/transcript.jsonl,
connects as a second client, calls turn/steer, prints the server's answer, and records it in LANE_DIR/steer.log."""

import re
import base64, json, os, socket, struct, sys, time


def main(argv):
    lane_dir, sock_path, text_file = argv[:3]
    text = open(text_file).read()
    thread = open(os.path.join(lane_dir, "thread.id")).read().strip()
    turn = None
    for line in open(os.path.join(lane_dir, "transcript.jsonl")):
        try:
            value = json.loads(line).get("value")
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("method") == "turn/started":
            turn = ((value.get("params") or {}).get("turn") or {}).get("id") or turn
    if not turn:
        sys.exit("TurnIdMissing: no turn/started in transcript")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(sock_path)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % key
        ).encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            sys.exit("HandshakeClosed")
        buf += chunk
    head, buf = buf.split(b"\r\n\r\n", 1)
    if b" 101 " not in head.split(b"\r\n")[0]:
        sys.exit("HandshakeRefused: " + head.decode(errors="replace"))

    def recv_exact(n):
        nonlocal buf
        while len(buf) < n:
            chunk = sock.recv(65536)
            if not chunk:
                sys.exit("SocketClosed")
            buf += chunk
        out, buf = buf[:n], buf[n:]
        return out

    def send(frame):
        payload = json.dumps(frame).encode()
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytes([0x81]) + (
            bytes([0x80 | n])
            if n < 126
            else bytes([0x80 | 126]) + struct.pack(">H", n)
            if n < 65536
            else bytes([0x80 | 127]) + struct.pack(">Q", n)
        )
        sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv():
        data = b""
        while True:
            b0, b1 = recv_exact(2)
            op = b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", recv_exact(8))[0]
            mk = recv_exact(4) if b1 & 0x80 else None
            payload = recv_exact(n)
            if mk:
                payload = bytes(b ^ mk[i % 4] for i, b in enumerate(payload))
            if op in (0x9, 0xA):
                continue
            data += payload
            if b0 & 0x80:
                return json.loads(data)

    def call(rid, method, params):
        send({"id": rid, "method": method, "params": params})
        while True:
            f = recv()
            if f.get("id") == rid and "method" not in f:
                return f

    call(
        "1",
        "initialize",
        {"clientInfo": {"name": "decomp_steer", "version": "1"}, "capabilities": {"experimentalApi": True}},
    )
    send({"method": "initialized"})
    answer = call(
        "2", "turn/steer", {"threadId": thread, "expectedTurnId": turn, "input": [{"type": "text", "text": text}]}
    )
    # Continuation turns started by lane.py log no turn/started; the server names the active turn in its refusal.
    active = re.search(r"but found `([^`]+)`", json.dumps(answer.get("error") or {}))
    if active:
        turn = active.group(1)
        answer = call(
            "3", "turn/steer", {"threadId": thread, "expectedTurnId": turn, "input": [{"type": "text", "text": text}]}
        )
    line = json.dumps({"at": time.time(), "turn": turn, "file": text_file, "answer": answer})
    open(os.path.join(lane_dir, "steer.log"), "a").write(line + "\n")
    # Durable copy: lanes re-read their brief after a context compaction, so an accepted steer joins it.
    brief = next((p for p in (os.path.join(lane_dir, n) for n in ("brief.md", "Brief.md")) if os.path.exists(p)), None)
    if brief and not answer.get("error"):
        stamp = time.strftime("%H:%M")
        open(brief, "a").write("\n\nSTEER (%s, %s):\n%s\n" % (stamp, os.path.basename(text_file), text.strip()))
    print(line)
    return 1 if answer.get("error") else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
