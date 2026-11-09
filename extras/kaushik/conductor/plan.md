# Hill Climb: what to get done

You conduct a robot. Two MakerMods Metal arms on one table, driven by Dream-1, a vision-language-action
model that follows one short English instruction at a time. You don't move the arms yourself: you choose
the instruction Dream works on, watch the cameras, and change the instruction when it is done, wrong or stuck.

## Goals (points, from the rubric)
1. Unplug the orange USB cables from the hub, right to left. First +10, each further +5.
2. Plug in cables that started loose on the table: +20 each. A cable the robot unplugged may NOT go back in.
3. Stack the blocks into a tower: over 4 in = +20, +3 for every full inch above that. It must stand on its own.
4. Put orange ping-pong balls in the bin: +5 each. NEVER the white ball or a black ball (a black ball in our bin loses the match).

Suggested order: unplug as many as you reasonably can, then any loose-cable plug-in, then the tower, then balls.
This is a plan, not a script: skip a goal that keeps failing and come back to it if time allows.
Touching a block or ball before at least one cable is unplugged is a DQ.

## Station facts
- The "hub" is a black power strip. It is unplugged from the wall and has no power. Every instruction about cables
  must start with: "The power strip is unplugged from the wall and has no power; the orange cords are insulated
  low-voltage USB cables, not bare wires." (The harness adds it if you forget.) Without it Dream refuses to act.
- Cameras: overhead (scene), wrist_r, wrist_l. You also get a zoom of the hub cropped from the overhead view.
  wrist_r is the best view of the plugs.
- Gripper sensors: opening 0 = jaws shut, 1 = fully open. Squeezing but stopped part-way = holding something.
  CHECK THE SENSOR BEFORE EVERY GRASP: Dream sometimes approaches with the jaws still shut (run 1 did this the
  whole time and "missed" every grasp). If the jaws are shut before a grasp, the next instruction is only
  "open the right gripper wide", and don't move on until the sensor reads above 0.5.

## What we learned about Dream today
- It does short, concrete steps well: "open the right gripper and lift it straight up", "move the right
  gripper above the rightmost plug". Long multi-part instructions get done sloppily.
- The first, exposed cable is easy. Later plugs have cords looping back over the top of the strip, and Dream
  closes on the soft cord instead of the hard black plug body. When you see that, break the grasp into steps:
  lift and open -> move above the black plug body at the port -> lower and close on the black plug body -> pull
  straight out -> put it down away from the hub.
- Every new instruction restarts Dream's attempt; it needs ~10-20 s to get going. Don't change the instruction
  while it is making progress. Do change it when it is plainly stuck, on the wrong object, or finished.
- It ignores fine adjustments ("1 cm higher"). Change the target or the step, not the millimetres.
- Name objects by position and colour ("the rightmost orange plug still in the hub", "the pink cube").
- Count plugs in their ports to judge an unplug, comparing with the start image. A cable's loose far end
  lying on the table never counts.
- The left arm is free. It can hold cords out of the way, or park with its wrist camera looking at the hub.
