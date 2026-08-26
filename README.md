# nigri-sync

Pushes BrightPath's daily attendance + points into the Nigri Jewish
Online School site with one click, instead of re-entering everything
by hand.

## Status: scaffold, not yet functional

I built the full request/response flow and the Playwright automation
skeleton, but I could not reach nigrijewishonlineschool.com from my
sandbox to inspect real HTML. Every `TODO_` in `nigri_playwright.py`
is a placeholder selector that needs to be swapped for the real one.

### What I need from you to finish it

1. Open the **login page**, right-click → View Page Source (or
   Inspect the username field, password field, and login button).
   Save/copy that HTML.
2. Open the **Attendance** page, expand one student row (like your
   screenshot), then Inspect:
   - the "Present" checkbox
   - the "Rewards" `<select>` dropdown
   - the Save/Update button
   - the period dropdown at the top ("Attendance for: ...")
   - the mini calendar (how a specific day gets clicked)
3. Confirm the exact period names/order for B3 WT and B3 ET (I guessed
   Davening / Chumash / Halacha as periods 1/2/3 — you know your real
   schedule).

Send me the HTML (or just paste the relevant `<form>`/`<select>`/
`<input>` snippets) and I'll fill in every TODO with the real selector.

## Deploy (once selectors are filled in)

Same pattern as `posuk-scorer`:

1. Push this folder to a GitHub repo.
2. New Render Web Service, Python environment.
3. Build command:
   ```
   pip install -r requirements.txt && playwright install --with-deps chromium
   ```
4. Start command:
   ```
   python app.py
   ```
5. Environment variables on Render:
   - `NIGRI_USERNAME`
   - `NIGRI_PASSWORD`
   - `SYNC_API_KEY` — make up any random string, e.g. `openssl rand -hex 16`

## Button in BrightPath

Add this to your teacher dashboard (wherever the Daily Points table
lives), swapping in your real Render URL and the same `SYNC_API_KEY`:

```html
<button id="syncNigriBtn">Sync to Nigri</button>
<span id="syncStatus"></span>

<script>
document.getElementById('syncNigriBtn').addEventListener('click', async () => {
  const statusEl = document.getElementById('syncStatus');
  statusEl.textContent = 'Syncing...';

  // Build this from whatever's currently on screen / in Firebase for the day
  const payload = {
    class_section: currentClassSection,   // "B3 WT" or "B3 ET"
    date: currentDateISO,                 // "2026-08-26"
    students: currentDayStudents.map(s => ({
      name: s.name,
      points: s.totalPoints              // BrightPath's already-computed total
    }))
  };

  try {
    const res = await fetch('https://YOUR-RENDER-URL.onrender.com/sync-points', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Sync-Key': 'YOUR_SYNC_API_KEY'
      },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    statusEl.textContent = res.ok ? '✅ Synced!' : `❌ ${data.detail || 'Failed'}`;
  } catch (err) {
    statusEl.textContent = '❌ ' + err.message;
  }
});
</script>
```

## Open questions to nail down together

- Exact period names/order for each class (I guessed at 3 periods —
  confirm real ones)
- Exact date-picker interaction (click day number vs type a date)
- Whether "points" on BrightPath's side is already the exact number
  you want in the 0–30 Rewards field, or needs any rounding/clamping
  since Nigri caps at 30
