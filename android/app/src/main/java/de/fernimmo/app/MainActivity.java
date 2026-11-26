package de.fernimmo.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.util.Base64;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.HttpAuthHandler;
import android.webkit.URLUtil;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.Toast;

import java.nio.charset.StandardCharsets;

/**
 * FernImmo als App: zeigt die Weboberfläche des eigenen Servers (im Heimnetz, per WireGuard)
 * an, merkt sich Server-Adresse und Login, öffnet Portal-Links im normalen Browser und lädt
 * Excel-/CSV-Exporte in den Download-Ordner.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "fernimmo";
    private static final String KEY_URL = "server_url";
    private static final String KEY_USER = "user";
    private static final String KEY_PASS = "pass";

    private WebView web;
    private ProgressBar progress;
    private SharedPreferences prefs;
    private boolean authFailedOnce = false;
    private boolean errorShown = false;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        progress.setVisibility(View.GONE);
        root.addView(progress, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 8));
        web = new WebView(this);
        root.addView(web, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportMultipleWindows(false);
        s.setUserAgentString(s.getUserAgentString() + " FernImmoApp/" + appVersion());
        CookieManager.getInstance().setAcceptCookie(true);

        web.setWebViewClient(new Client());
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onProgressChanged(WebView view, int p) {
                progress.setProgress(p);
                progress.setVisibility(p < 100 ? View.VISIBLE : View.GONE);
            }
        });
        web.setDownloadListener((url, userAgent, contentDisposition, mimeType, length) ->
                download(url, userAgent, contentDisposition, mimeType));

        if (state != null) {
            web.restoreState(state);
        } else if (serverUrl().isEmpty()) {
            showSettings(true);
        } else {
            web.loadUrl(serverUrl());
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    private String appVersion() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "?";
        }
    }

    // ------------------------------------------------------------------ Einstellungen
    private String serverUrl() {
        return prefs.getString(KEY_URL, "");
    }

    private static String normalize(String url) {
        String u = url.trim();
        if (u.isEmpty()) return u;
        if (!u.startsWith("http://") && !u.startsWith("https://")) u = "http://" + u;
        while (u.endsWith("/")) u = u.substring(0, u.length() - 1);
        return u;
    }

    private void showSettings(boolean firstStart) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        box.setPadding(pad, pad / 2, pad, 0);

        EditText url = new EditText(this);
        url.setHint("http://192.168.178.10:8080");
        url.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        url.setText(serverUrl());
        box.addView(url);
        EditText user = new EditText(this);
        user.setHint("Benutzername (falls Login aktiv)");
        user.setText(prefs.getString(KEY_USER, ""));
        box.addView(user);
        EditText pass = new EditText(this);
        pass.setHint("Passwort");
        pass.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        pass.setText(prefs.getString(KEY_PASS, ""));
        box.addView(pass);

        AlertDialog.Builder b = new AlertDialog.Builder(this)
                .setTitle(firstStart ? "FernImmo einrichten" : "App-Einstellungen")
                .setMessage("Adresse deines FernImmo-Servers, wie sie über WireGuard erreichbar ist.")
                .setView(box)
                .setPositiveButton("Speichern", (d, w) -> {
                    String u = normalize(url.getText().toString());
                    if (!URLUtil.isNetworkUrl(u)) {
                        Toast.makeText(this, "Ungültige Adresse", Toast.LENGTH_LONG).show();
                        showSettings(firstStart);
                        return;
                    }
                    prefs.edit().putString(KEY_URL, u)
                            .putString(KEY_USER, user.getText().toString().trim())
                            .putString(KEY_PASS, pass.getText().toString())
                            .apply();
                    authFailedOnce = false;
                    errorShown = false;
                    web.clearHistory();
                    web.loadUrl(u);
                });
        if (!firstStart) {
            b.setNeutralButton("Neu laden", (d, w) -> web.reload());
            b.setNegativeButton("Abbrechen", null);
        } else {
            b.setCancelable(false);
        }
        b.show();
    }

    // ------------------------------------------------------------------ Downloads
    private void download(String url, String userAgent, String contentDisposition, String mimeType) {
        try {
            DownloadManager.Request r = new DownloadManager.Request(Uri.parse(url));
            String name = URLUtil.guessFileName(url, contentDisposition, mimeType);
            r.setMimeType(mimeType);
            r.addRequestHeader("User-Agent", userAgent);
            String cookies = CookieManager.getInstance().getCookie(url);
            if (cookies != null) r.addRequestHeader("Cookie", cookies);
            String user = prefs.getString(KEY_USER, "");
            if (!user.isEmpty()) {
                String token = Base64.encodeToString(
                        (user + ":" + prefs.getString(KEY_PASS, "")).getBytes(StandardCharsets.UTF_8), Base64.NO_WRAP);
                r.addRequestHeader("Authorization", "Basic " + token);
            }
            r.setTitle(name);
            r.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            r.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(r);
            Toast.makeText(this, "Download gestartet: " + name, Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            Toast.makeText(this, "Download fehlgeschlagen: " + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    // ------------------------------------------------------------------ WebView
    private class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri uri = request.getUrl();
            if ("fernimmo-app".equals(uri.getScheme())) {
                showSettings(false);
                return true;
            }
            Uri home = Uri.parse(serverUrl());
            boolean sameServer = home.getHost() != null && home.getHost().equals(uri.getHost())
                    && home.getPort() == uri.getPort();
            if (sameServer) return false;
            // Portal-Links, Karten-Attribution usw. im normalen Browser öffnen
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, uri));
            } catch (ActivityNotFoundException e) {
                Toast.makeText(MainActivity.this, "Keine App für " + uri, Toast.LENGTH_LONG).show();
            }
            return true;
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            errorShown = false;
        }

        @Override
        public void onReceivedHttpAuthRequest(WebView view, HttpAuthHandler handler, String host, String realm) {
            String user = prefs.getString(KEY_USER, "");
            if (!user.isEmpty() && !authFailedOnce) {
                authFailedOnce = true;   // zweiter Aufruf = Login falsch -> Einstellungen zeigen
                handler.proceed(user, prefs.getString(KEY_PASS, ""));
            } else {
                handler.cancel();
                Toast.makeText(MainActivity.this, "Login nötig oder falsch", Toast.LENGTH_LONG).show();
                authFailedOnce = false;
                showSettings(false);
            }
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (!url.startsWith("data:")) authFailedOnce = false;
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (!request.isForMainFrame() || errorShown) return;
            errorShown = true;
            new AlertDialog.Builder(MainActivity.this)
                    .setTitle("Server nicht erreichbar")
                    .setMessage("FernImmo antwortet nicht unter\n" + serverUrl()
                            + "\n\nIst WireGuard verbunden?\n(" + error.getDescription() + ")")
                    .setPositiveButton("Erneut versuchen", (d, w) -> web.reload())
                    .setNeutralButton("Adresse ändern", (d, w) -> showSettings(false))
                    .setNegativeButton("Schließen", null)
                    .show();
        }
    }
}
