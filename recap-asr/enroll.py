"""Build the owner voice reference for recap-asr from outgoing call recordings.

Usage:
    python enroll.py call1.m4a call2.m4a call3.m4a [-o owner_ref.npy]

Give it 2-3 outgoing calls to DIFFERENT contacts. Each call is diarized into
two speakers; the speaker whose voice embedding repeats across all calls is
the phone's owner. The mean embedding of that matched group is saved as the
reference used by server.py for the is_owner flag.
"""

import argparse
import itertools
import os
import sys

import numpy as np

import server  # reuses model loading, diarize(), speaker_embedding()
from server import decode_audio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", help="outgoing call recordings, different contacts")
    ap.add_argument("-o", "--output", default=server.OWNER_REF)
    args = ap.parse_args()

    if len(args.files) < 2:
        sys.exit("need at least 2 calls to cross-match the common speaker")

    per_call = []  # list of [(label, emb, talk_time), ...] per call
    for f in args.files:
        print(f"diarizing {os.path.basename(f)} ...")
        audio = decode_audio(f, sampling_rate=server.SAMPLE_RATE)
        ann, speakers = server.diarize(audio, num_speakers=2)
        embs = [(s["label"], s["embedding"], s["talk_time"]) for s in speakers if s["embedding"] is not None]
        if not embs:
            sys.exit(f"no usable speech in {f}")
        per_call.append(embs)
        for label, _, talk in embs:
            print(f"  {label}: {talk:.0f}s of speech")

    # pick one speaker per call so that the group is maximally self-similar
    best_group, best_score = None, -1.0
    for combo in itertools.product(*per_call):
        pairs = itertools.combinations(combo, 2)
        sims = [server._cosine(a[1], b[1]) for a, b in pairs]
        score = float(np.mean(sims))
        if score > best_score:
            best_group, best_score = combo, score

    print("\ncross-call match:")
    for f, (label, _, talk) in zip(args.files, best_group):
        print(f"  {os.path.basename(f)}: {label} ({talk:.0f}s)")
    print(f"mean pairwise similarity of the group: {best_score:.3f}")
    if best_score < 0.4:
        print("WARNING: similarity is low — the common speaker is uncertain.")
        print("Use calls to different contacts where the owner clearly speaks.")

    ref = np.mean([emb for _, emb, _ in best_group], axis=0)
    np.save(args.output, ref)
    print(f"\nowner reference saved to {args.output}")

    # sanity check: per-call similarity of the chosen speaker vs the reference,
    # and of the OTHER speaker vs the reference (should be clearly lower)
    print("\nsanity check (owner vs other, per call):")
    for f, embs, chosen in zip(args.files, per_call, best_group):
        own = server._cosine(chosen[1], ref)
        others = [server._cosine(e, ref) for lbl, e, _ in embs if lbl != chosen[0]]
        other = max(others) if others else float("nan")
        print(f"  {os.path.basename(f)}: owner {own:.3f} vs other {other:.3f}")


if __name__ == "__main__":
    main()
