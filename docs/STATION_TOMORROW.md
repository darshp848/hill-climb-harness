# At the station: grip strength and live joint telemetry

Both need the NUC (`pah@10.103.0.143`, you type the password). Neither changes how Dream's agent drives the arms
except step 1, which is Dream's own setting.

## 1. Grip at max strength (Darsh's notes: "grip at max strength", "peak strength")

The Metal gripper's grip force is `gripper_hold_torque_nm` in `~/.dreamlabs/station.yaml` (Dream's
`robots.makers_hold` law: the target is regulated so the measured torque sits at this value). Dream's default is
0.5 N.m. The station already runs **1.5** on both arms (changed 7 Nov, backup `station.yaml.bak-before-holdtorque`).
Dream's config accepts at most **2.0** (`MAX_HOLD_TORQUE_NM`).

```bash
ssh pah@10.103.0.143
cp ~/.dreamlabs/station.yaml ~/.dreamlabs/station.yaml.bak-before-hold2
sed -i 's/gripper_hold_torque_nm: 1.5.*/gripper_hold_torque_nm: 2.0  # max Dream accepts (was 1.5)/' ~/.dreamlabs/station.yaml
grep -n gripper_hold_torque_nm ~/.dreamlabs/station.yaml      # both arms should say 2.0
dreamcli robot up                                              # restart the station so it reads the file
```

Then watch the gripper temperature with step 2 while it holds a plug: the gripper latches off at 55 C
(`robots.metal_gripper.STOP_C`) and refuses to start above 45 C. Going past 2.0 means patching Dream's package
constant; only consider it if 2.0 still lets plugs slip and the temperature stays well under 45 C.

## 2. Live joint torque, position and commanded torque (harness/can_live.py)

The arms are on SocketCAN (`can_follower_l`, `can_follower_r`, station.log 7 Nov), so a receive-only listener
sees Dream's MIT commands and the motors' replies. It never sends a frame.

```bash
# from this laptop (Git Bash), copy the two files:
scp harness/can_live.py harness/can_torque.py pah@10.103.0.143:~/
# on the NUC:
python3 ~/can_live.py --serve 0.0.0.0:8097 --arm left=can_follower_l --arm right=can_follower_r
# it prints reply rates every 5 s (about 100+ Hz per arm while Dream drives it)
```

Then run the harness with `--joints http://10.103.0.143:8097`. The monitor's gripper note gains measured grip
("right gripper is squeezing an object (1.9 of 20 N.m)"), and the run log gets `joint_alert` events (strain,
blocked, outside force). Check it once by hand: `curl http://10.103.0.143:8097/joints`.
