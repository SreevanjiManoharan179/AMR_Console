#!/usr/bin/env bash
# Laptop microphone check for the AMR Console (8 Nov 2026), PipeWire tools only (wpctl, arecord).
# Records four 5-second takes from the laptop's own input at four boost/volume pairs, and a report.
# Changes nothing permanently: the default microphone (WO Mic), and the laptop input's volume and mute,
# are put back at the end. Output: data/evaluation/mic_check/ (report.txt, take_*.wav).
#
#   bash scripts/mic_check.sh           the four test takes
#   bash scripts/mic_check.sh --apply   set the laptop mic to the setting that measured best on 10 Nov 2026
#                                       (boost 2 of 3, input volume 0.30: no clipping, voice 15 dB over noise)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HERE="$ROOT/data/evaluation/mic_check"
mkdir -p "$HERE"
REPORT="$HERE/report.txt"
: > "$REPORT"
section() { printf '\n===== %s =====\n' "$1" >> "$REPORT"; }

audio_sources() {  # one line per audio source: id <TAB> name <TAB> * if default
  wpctl status | awk '
    /^Audio/ {a = 1} /^Video/ {a = 0}
    a && /Sources:/ {s = 1; next}
    a && /Source endpoints:|Streams:|Filters:|Sinks:/ {s = 0}
    s && /[0-9]+\./ {
      d = ($0 ~ /\*/) ? "*" : ""; line = $0; sub(/^[^0-9]*/, "", line)
      id = line; sub(/\..*/, "", id); name = line; sub(/^[0-9]+\. */, "", name); sub(/ *\[.*$/, "", name)
      print id "\t" name "\t" d }'
}

section "date"; date >> "$REPORT"
section "sound cards (/proc/asound/cards)"; cat /proc/asound/cards >> "$REPORT" 2>&1
section "capture devices (arecord -l)"; arecord -l >> "$REPORT" 2>&1
section "kernel audio drivers loaded"; (lsmod | grep -E '^snd_(sof|hda_intel|soc|intel)' ) >> "$REPORT" 2>&1
section "audio sources (id, name, default)"; audio_sources >> "$REPORT"
for card in $(awk '/^ *[0-9]+ \[/{print $1}' /proc/asound/cards); do
  section "mixer controls, card $card (mic and capture only)"
  amixer -c "$card" scontents 2>&1 | awk '/^Simple mixer control/{keep = ($0 ~ /[Mm]ic|[Cc]apture|[Ii]nput|[Dd]igital/)} keep' >> "$REPORT"
done

BUILTIN="$(audio_sources | awk -F'\t' '$2 ~ /Built-in|Internal|Digital Mic|Analog/ && $2 !~ /Loopback|WO Mic/ {print $1; exit}')"
DEFAULT="$(audio_sources | awk -F'\t' '$3 == "*" {print $1; exit}')"
section "laptop input used for the takes"; echo "id ${BUILTIN:-none}; default before: ${DEFAULT:-none}" >> "$REPORT"
if [ -z "$BUILTIN" ]; then
  echo "No laptop input found. Report written to $REPORT"; exit 1
fi
wpctl inspect "$BUILTIN" 2>&1 | grep -E 'node.name|node.description|api.alsa.path|api.alsa.pcm|device.profile|alsa.card_name|alsa.id' >> "$REPORT"

if [ "${1:-}" = "--apply" ]; then
  amixer -q -c PCH sset 'Internal Mic Boost' 2
  wpctl set-mute "$BUILTIN" 0
  wpctl set-volume "$BUILTIN" 0.30
  echo "Laptop microphone set: boost 2 of 3, input volume 0.30, unmuted. The default microphone is unchanged."
  exit 0
fi
OLD="$(wpctl get-volume "$BUILTIN")"          # e.g. "Volume: 1.00 [MUTED]"
OLD_VOL="$(echo "$OLD" | awk '{print $2}')"
OLD_MUTED=0; echo "$OLD" | grep -q MUTED && OLD_MUTED=1
echo "laptop input before: $OLD" >> "$REPORT"
restore() {
  wpctl set-volume "$BUILTIN" "${OLD_VOL:-1.0}"
  wpctl set-mute "$BUILTIN" "$OLD_MUTED"
  [ -n "$DEFAULT" ] && wpctl set-default "$DEFAULT"
}
wpctl set-default "$BUILTIN"
wpctl set-mute "$BUILTIN" 0
sleep 1

# Analog boost on the laptop mic (card PCH, 0-3 = 0, +10, +20, +30 dB) paired with an input volume.
CARD=PCH
OLD_BOOST="$(amixer -c "$CARD" sget 'Internal Mic Boost' 2>/dev/null | grep -o 'Front Left: [0-9]*' | awk '{print $3}')"
echo "internal mic boost before: ${OLD_BOOST:-unknown}" >> "$REPORT"
restore_boost() { [ -n "$OLD_BOOST" ] && amixer -q -c "$CARD" sset 'Internal Mic Boost' "$OLD_BOOST"; }
trap 'restore_boost; restore' EXIT
n=1
for setting in "0 1.00" "1 0.60" "2 0.35" "3 0.15"; do
  set -- $setting
  amixer -q -c "$CARD" sset 'Internal Mic Boost' "$1" 2>/dev/null
  wpctl set-volume "$BUILTIN" "$2"
  file="$HERE/take_${n}_boost$1_vol$2.wav"
  echo
  echo "Take $n of 4 (boost $1, volume $2). Stay quiet for the first second, then say: \"Give the file to Divya\""
  sleep 1
  echo "  ...recording 5 s now (quiet first)"
  arecord -q -d 5 -f S16_LE -r 16000 -c 1 "$file"
  echo "take $n: boost $1, volume $2 -> $(basename "$file")" >> "$REPORT"
  n=$((n + 1))
done
echo
echo "Done. Default microphone and the laptop input's volume and mute are put back."
echo "Files in $HERE: report.txt and take_1..4_*.wav. Boost put back to ${OLD_BOOST:-its old value}."
