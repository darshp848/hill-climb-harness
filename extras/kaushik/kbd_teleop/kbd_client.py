"""Keyboard teleop panel: sends keys to kbd_collect.py on the station (via `ssh -L 8095:localhost:8095 ...`).
Ctrl+C quits."""
import socket, sys, termios, threading, tty, time

B, D, R, G, Y, C, X = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m"
state = {"arm": "?", "drive": "off", "gripper": "0.00", "step_mm": "5"}
events = []
lock = threading.Lock()

def key(k, what):
    return f"{B}{C}[{k:^5}]{X} {what}"

def draw():
    drive = state["drive"] == "on"
    g = float(state["gripper"])
    gtxt = f"{R}{B}CLOSED{X}" if g >= 0.99 else (f"{G}{B}OPEN{X}" if g <= 0.01 else f"{Y}{B}{int(g * 100)}% closed{X}")
    bar = (f"  ARM {B}{state['arm'].upper()}{X}    "
           f"{(G + B + '● DRIVING' + X) if drive else (D + '○ holding (press space to drive)' + X)}    "
           f"GRIPPER {gtxt}    STEP {B}{state['step_mm']} mm{X}")
    out = ["\033[H\033[2J", f"{B} KEYBOARD TELEOP  (this window must be focused; keys repeat when held){X}", "",
           bar, "", f"{B} MOVE (in the arm's own directions){X}",
           f"   {key('w', 'forward')}      {key('r', 'up')}",
           f"   {key('s', 'back')}         {key('f', 'down')}",
           f"   {key('a', 'left')}         {key('-', 'smaller step (2 mm)')}",
           f"   {key('d', 'right')}        {key('=', 'bigger step (10 mm)')}", "",
           f"{B} TURN THE WRIST (3 deg per press){X}",
           f"   {key('j', 'roll left')}    {key('l', 'roll right')}",
           f"   {key('i', 'pitch up')}     {key('k', 'pitch down')}",
           f"   {key('u', 'yaw left')}     {key('o', 'yaw right')}", "",
           f"{B} HAND & SESSION{X}",
           f"   {key('space', 'drive on / off')}       {key('g', 'gripper close / open')}",
           f"   {key('[ ]', 'gripper -/+ 25%')}        {key('1 2', 'left / right arm')}",
           f"   {key('Enter', 'A: open arms / START or SAVE episode')}",
           f"   {key('x', 'B: DISCARD episode')}       {key('h', 'home (not while recording)')}", "",
           f"{B} LAST{X}"] + [f"   {e}" for e in events[-5:]]
    sys.stdout.write("\n".join(out) + "\n"); sys.stdout.flush()

def listen(sock):
    for line in sock.makefile():
        line = line.strip()
        with lock:
            if line.startswith("STATE "):
                state.update(dict(kv.split("=", 1) for kv in line[6:].split()))
            elif line.startswith("EVENT "):
                events.append(time.strftime("%H:%M:%S ") + line[6:])
            draw()
    with lock:
        events.append(R + "disconnected from the station (restart this panel)" + X); draw()

s = socket.create_connection(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8095))
threading.Thread(target=listen, args=(s,), daemon=True).start()
fd = sys.stdin.fileno(); old = termios.tcgetattr(fd)
try:
    tty.setcbreak(fd)
    while (ch := sys.stdin.read(1)) and ch != "\x03":
        s.sendall(ch.encode())
except KeyboardInterrupt:
    pass
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
    sys.stdout.write(X + "\n")
