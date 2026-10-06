name: Qantas deal monitor

# One run loops for about 5.5 hours, checking every few minutes. A new run is
# queued every 2 hours and starts the moment the current one finishes, so the
# monitor is effectively always on. Queued runs that get replaced show as
# "cancelled" in the Actions tab. That is expected.

on:
  schedule:
    - cron: "17 */2 * * *"
  workflow_dispatch:

concurrency:
  group: qantas-deal-monitor
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  watch:
    runs-on: ubuntu-latest
    timeout-minutes: 345
    env:
      NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}
      MAX_CENTS_PER_POINT: "1.0"     # alert at or below this cost per point
      CHECK_EVERY_SECONDS: "180"     # 180 = every 3 minutes
      RUN_MINUTES: "330"
    steps:
      - uses: actions/checkout@v4

      - name: Restore which deals were already alerted
        uses: actions/cache/restore@v4
        with:
          path: state.json
          key: state-${{ github.run_id }}
          restore-keys: state-

      - name: Watch for deals
        run: python3 monitor.py

      - name: Save which deals were already alerted
        if: always()
        uses: actions/cache/save@v4
        with:
          path: state.json
          key: state-${{ github.run_id }}
