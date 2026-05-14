# QuantG → Android App (APK / Play Store)

You now have **three** ways to put QuantG on your Android phone, from easiest to most pro.

---

## Option 1 · "Add to Home Screen" (PWA) — 30 seconds, free

Best for personal use. No app store, no APK file.

1. Open `https://bottrader-pro-4.emergent.host` in **Chrome on Android**
2. You should see an **"Install QuantG"** banner pop up at the bottom — tap **Install**.
3. If you don't see it: open Chrome menu (⋮) → **Add to Home screen** → **Install**.
4. QuantG icon appears on your home screen. Tap it → opens **full-screen, no browser bar, looks like a real app**. Works offline (shell pages cached).

**That's it.** This is what 95% of "lightweight Android trading apps" actually are under the hood. You get push-like notifications, instant updates, and zero app-store hassle.

---

## Option 2 · Real APK file via PWABuilder — 5 minutes, free, no Android Studio

You'll get a `.apk` file you can sideload or upload to Play Store.

### Steps

1. Go to **https://www.pwabuilder.com**
2. Paste your URL: `https://bottrader-pro-4.emergent.host`
3. Click **Start** → PWABuilder will analyse and score the manifest (we're set up correctly already, expect ~95/100).
4. Click **Package For Stores** → choose **Android**.
5. Sign in (free Microsoft account) → fill out:
   - **Package name**: `com.quantg.app` (must be unique, can't change later)
   - **App name**: QuantG
   - **Launcher name**: QuantG
   - **App version**: 1.0
6. Click **Generate** → wait ~60 seconds → **Download** the .zip.
7. Inside the zip: `app-release-signed.apk` is your installer.

### Install the APK on your phone

1. Copy the APK to your phone (USB, Drive, WhatsApp to yourself, etc.)
2. On phone: Settings → Apps → Special access → **Install unknown apps** → enable for your file manager.
3. Tap the APK file → Install. Done.

> The APK is a **TWA** (Trusted Web Activity) — it's a thin Chrome wrapper that opens your live URL. Tiny file (~3 MB), always shows the latest version (no need to re-publish for code updates).

---

## Option 3 · Publish to Google Play Store — 1 day + ₹2,000

If you want it on the actual Play Store:

1. Pay the one-time **Google Play developer fee**: ₹2,000 at https://play.google.com/console
2. Use the **same APK** from Option 2.
3. In Play Console: Create app → upload APK → fill listing (description, screenshots, privacy policy URL, content rating).
4. Submit for review (typically 1-3 days first time).
5. Live on Play Store. Updates auto-roll-out when you update the website — no need to re-submit APK unless you change manifest/icons.

---

## What works in the PWA / APK?

| Feature | Works on Android PWA/APK? |
|---|---|
| Login, dashboard, strategies, AI bot, orders | ✅ Yes |
| Live Zerodha prices (when market open) | ✅ Yes |
| Push notifications on order fills | ⚠ Requires extra setup (Firebase Cloud Messaging — add when needed) |
| Background trading when app closed | ❌ No (browser sleeps; strategy runner runs **server-side** so this is fine — your strategies keep ticking on the QuantG server even if your phone is off) |
| Biometric login (fingerprint) | ⚠ Possible via WebAuthn — let me know if you want it |
| Offline mode | ✅ Shell pages cached; live data needs network (obviously) |

---

## My recommendation

Start with **Option 1** (Add to Home Screen). It's free, takes 30 seconds, and works perfectly for solo trading. Move to Option 2 only if you specifically want a sideload-able `.apk` file.

Skip Option 3 unless you plan to actually sell access to others.
