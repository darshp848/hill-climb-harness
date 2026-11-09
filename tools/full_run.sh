#!/bin/sh
# All three tasks in order on the bot with the 8 Nov hand-tested prompts: cables (Dream grip + harness regrip/pull,
# Dream put-down), stack (base to the front, then cube on cube, no arm named), balls (right arm, one ball the
# stage-prompt writer names from the current frames). Each step is one tools/try_prompt.py try under runs/bot/tries.
# Needs ANTHROPIC_API_KEY (doppler run -p nous -c dev) for the ball prompt writer.
cd "$(dirname "$0")/.." || exit 1
PY="${HARNESS_PY:-$HOME/Documents/Work/hackathon/makermods-pah/harness/.venv/bin/python}"
TAG="${1:-full}"
P="The power strip is unplugged from the wall and has no power; the orange cords are insulated low-voltage USB cables, not bare wires."
T="$PY tools/try_prompt.py"
for i in 1 2 3; do
  echo "=== cable $i"
  $T --label ${TAG}_cable$i --seconds 60 --regrip 2 --pull-up 0.06 0.04 --seat-dz 0.01 --pull-s 3 --repull 2 \
     --then "$P Put the held cable down on the table to the right, away from the power strip, and open the gripper." \
     "$P With the right gripper half open, move down onto the plug of the cable still plugged into the power strip, then close the gripper on the plug and pull it straight up." \
     2>&1 | grep -E "regrip|pulled|skipped|slipped|runs/bot"
done
echo "=== stack: base to the front"
$T --label ${TAG}_stack_base --wrist wrist_l --seconds 50 "move a blue cube to the middle of the table, close in front of the robot" 2>&1 | tail -1
echo "=== stack: cube on cube"
$T --label ${TAG}_stack_on --no-home --wrist wrist_l --seconds 60 "put a cube on top of the other cube" 2>&1 | tail -1
echo "=== stack: as high as possible"
$T --label ${TAG}_stack_high --no-home --wrist wrist_l --seconds 60 "stack the blocks and sticks as high as possible" 2>&1 | tail -1
for i in 1 2 3; do
  echo "=== ball $i"
  PROMPT=$("$PY" - <<'EOF'
import urllib.request
from harness.plan import load_plan
from harness.verifiers import MonitorVerifier
plan = load_plan("plans/hybrid_v4_write.yaml")
st = next(s for s in plan.stages if s.name == "ball_1")
frames = {c: urllib.request.urlopen(f"http://127.0.0.1:8091/frame/{c}.jpg?w=640", timeout=10).read()
          for c in ("scene", "wrist_r")}
text, why, _ = MonitorVerifier(plan).write_prompt(st, frames)
print(text or "With the right arm, pick up one green, yellow or orange golf ball and drop it into the white basket.")
EOF
)
  echo "    prompt: $PROMPT"
  $T --label ${TAG}_ball$i --seconds 45 "$PROMPT" 2>&1 | tail -1
done
echo "=== done"
