# Benchmark

Benchmark data and evaluation tooling for Android → HarmonyOS migration quality.

## Structure

- **Android_repos/** — Source repos for 3 open-source Android apps (AntennaPod, Meshtastic, Simple-Gallery), used as the functional reference for migration evaluation.
- **feature_checklists/** — Feature checklists (.xlsx) for the 3 apps, listing the feature points to be evaluated.
- **evaluation_skill/** — The `a2h-evaluate` skill. Given an Android repo, a HarmonyOS repo, and a feature checklist as inputs, it reports functional completeness (Agent) and code alignment (Align) scores. The Align score requires CodeGraph (1.4+) to parse both repos into a shared graph schema.
