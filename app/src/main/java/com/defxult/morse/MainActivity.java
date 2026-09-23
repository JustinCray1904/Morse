package com.defxult.morse;

import android.Manifest;
import android.app.AlertDialog;
import android.app.PendingIntent;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.Settings;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.ImageButton;
import android.widget.ImageView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends AppCompatActivity {

    private static final String TAG = "morse";
    private static final int REQ_NOTIF = 1001;
    private static final int REQ_STORAGE_LEGACY = 1002;

    private View idleView, runningView;
    private TextView statusText, urlText;
    private View statusDot;
    private ImageView qrImage;
    private ImageButton btnSettings;
    private Button btnStart, btnStop, btnCopyUrl, btnOpenBrowser;

    private final ExecutorService bg = Executors.newSingleThreadExecutor();
    private boolean running = false;
    private boolean awaitingReturn = false;
    private String currentUrl;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        ThemeManager.applyNightMode(this);
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        bindViews();
        wireListeners();

        if (!hasStoragePermission()) {
            if (statusText != null) statusText.setText("Waiting for file access\u2026");
            askForStorage();
            return;
        }

        // Start CPython on a background thread. It's slow the first time.
        bg.execute(() -> {
            try {
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(getApplicationContext()));
                }
            } catch (Exception e) {
                Log.e(TAG, "Python.start failed", e);
            }
            runOnUiThread(() -> {
                maybeAskNotifications();
                refreshState();
            });
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (awaitingReturn) {
            awaitingReturn = false;
            if (hasStoragePermission()) {
                restartApp();
                return;
            }
        }
        if (Python.isStarted()) {
            applyAccent();
            refreshState();
        }
    }

    private void restartApp() {
        try {
            android.app.AlarmManager am =
                    (android.app.AlarmManager) getSystemService(Context.ALARM_SERVICE);
            Intent intent = new Intent(this, MainActivity.class);
            intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
            int piFlags = PendingIntent.FLAG_CANCEL_CURRENT;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) piFlags |= PendingIntent.FLAG_IMMUTABLE;
            PendingIntent pi = PendingIntent.getActivity(this, 0, intent, piFlags);
            if (am != null) {
                am.set(android.app.AlarmManager.RTC, System.currentTimeMillis() + 200, pi);
            }
        } catch (Exception ignored) {}
        android.os.Process.killProcess(android.os.Process.myPid());
        System.exit(0);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        bg.shutdownNow();
    }

    // ---------- view setup ----------

    private void bindViews() {
        idleView = findViewById(R.id.idle_view);
        runningView = findViewById(R.id.running_view);
        statusText = findViewById(R.id.statusText);
        statusDot = findViewById(R.id.statusDot);
        urlText = findViewById(R.id.urlText);
        qrImage = findViewById(R.id.qrImage);
        btnSettings = findViewById(R.id.btnSettings);
        btnStart = findViewById(R.id.btnStart);
        btnStop = findViewById(R.id.btnStop);
        btnCopyUrl = findViewById(R.id.btnCopyUrl);
        btnOpenBrowser = findViewById(R.id.btnOpenBrowser);
    }

    private void wireListeners() {
        btnSettings.setOnClickListener(v ->
                startActivity(new Intent(this, SettingsActivity.class)));

        btnStart.setOnClickListener(v -> onStartTapped());

        btnStop.setOnClickListener(v -> onStopTapped());

        btnCopyUrl.setOnClickListener(v -> copyToClipboard("morse URL", currentUrl));

        btnOpenBrowser.setOnClickListener(v -> {
            if (currentUrl == null) return;
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(currentUrl)));
            } catch (Exception e) {
                toast("No browser available");
            }
        });
    }

    private void applyAccent() {
        int a = ThemeManager.accentColor(this);
        ThemeManager.stylePrimaryButton(btnStart, a);
        ThemeManager.styleDangerButton(btnStop);
        ThemeManager.styleSecondaryButton(btnCopyUrl, a);
        ThemeManager.styleSecondaryButton(btnOpenBrowser, a);
        ThemeManager.tintText(urlText, a);
        ThemeManager.tintDot(statusDot, a);
        ThemeManager.tintBorder(qrImage, a, 2f);
        btnSettings.setColorFilter(a);
    }

    // ---------- state ----------

    private void refreshState() {
        running = ServerController.checkResume(this);
        if (running) {
            currentUrl = ServerController.getBoundUrl();
            if (currentUrl == null) {
                // Prefs said running and the port is live, but our in-memory
                // URL is gone (process was recreated). Rebuild it minimally.
                String ip = NetworkCheck.check().ip;
                if (ip != null) {
                    currentUrl = "http://" + ip + ":" + ServerController.getBoundPort() + "/";
                }
            }
            showRunning();
        } else {
            currentUrl = null;
            showIdle();
        }
    }

    private void showIdle() {
        idleView.setVisibility(View.VISIBLE);
        runningView.setVisibility(View.GONE);
        btnStart.setEnabled(true);
        btnStart.setText(R.string.start_server);
    }

    private void showRunning() {
        idleView.setVisibility(View.GONE);
        runningView.setVisibility(View.VISIBLE);
        statusText.setText(getString(R.string.running_on_port, ServerController.getBoundPort()));
        urlText.setText(currentUrl == null ? "" : currentUrl);
        renderQr(currentUrl);
    }

    private void renderQr(String url) {
        if (url == null) { qrImage.setVisibility(View.GONE); return; }
        try {
            Python py = Python.getInstance();
            PyObject mod = py.getModule("defxult_transfer");
            PyObject png = mod.callAttr("qr_png_bytes", url);
            if (png == null) { qrImage.setVisibility(View.GONE); return; }
            byte[] bytes = png.toJava(byte[].class);
            if (bytes == null || bytes.length == 0) {
                qrImage.setVisibility(View.GONE);
                return;
            }
            Bitmap bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.length);
            if (bmp == null) { qrImage.setVisibility(View.GONE); return; }
            qrImage.setImageBitmap(bmp);
            qrImage.setVisibility(View.VISIBLE);
        } catch (Exception e) {
            Log.w(TAG, "QR render failed", e);
            qrImage.setVisibility(View.GONE);
        }
    }

    // ---------- start / stop ----------

    private void onStartTapped() {
        if (!hasStoragePermission()) { askForStorage(); return; }
        if (!Python.isStarted()) { toast("Still starting up…"); return; }

        btnStart.setEnabled(false);
        btnStart.setText(R.string.checking);

        bg.execute(() -> {
            NetworkCheck.Result nr = NetworkCheck.check();
            if (!nr.ok) {
                runOnUiThread(() -> {
                    btnStart.setEnabled(true);
                    btnStart.setText(R.string.start_server);
                    showNoNetworkDialog();
                });
                return;
            }

            ServerController.StartResult sr = ServerController.start(getApplicationContext());
            runOnUiThread(() -> {
                btnStart.setEnabled(true);
                btnStart.setText(R.string.start_server);
                if (!sr.ok) {
                    toast("Start failed: " + sr.error);
                    return;
                }
                currentUrl = sr.url;
                try {
                    ServerService.start(MainActivity.this, sr.port);
                    Log.i(TAG, "foreground service start requested");
                } catch (Exception fgs) {
                    Log.e(TAG, "FGS start failed", fgs);
                    toast("Background service failed: " + fgs.getMessage());
                }
                running = true;
                applyAccent();
                showRunning();
            });
        });
    }

    private void onStopTapped() {
        bg.execute(() -> {
            ServerController.stop(getApplicationContext());
            ServerService.stop(MainActivity.this);
            runOnUiThread(() -> {
                running = false;
                currentUrl = null;
                showIdle();
            });
        });
    }

    // ---------- dialogs ----------

    private void showNoNetworkDialog() {
        new AlertDialog.Builder(this)
                .setTitle(R.string.no_network_title)
                .setMessage(R.string.no_network_msg)
                .setCancelable(false)
                .setPositiveButton(R.string.open_settings, (d, w) -> {
                    try {
                        startActivity(NetworkCheck.hotspotSettingsIntent(this));
                    } catch (Exception e) {
                        toast("Cannot open settings");
                    }
                })
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private void askForStorage() {
        new AlertDialog.Builder(this)
                .setTitle(R.string.storage_needed_title)
                .setMessage(R.string.storage_needed_msg)
                .setCancelable(false)
                .setPositiveButton(R.string.open_settings, (d, w) -> {
                    awaitingReturn = true;
                    Intent i;
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                        i = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION);
                        i.setData(Uri.parse("package:" + getPackageName()));
                    } else {
                        i = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
                        i.setData(Uri.parse("package:" + getPackageName()));
                        ActivityCompat.requestPermissions(this,
                                new String[]{Manifest.permission.READ_EXTERNAL_STORAGE},
                                REQ_STORAGE_LEGACY);
                    }
                    try {
                        startActivity(i);
                    } catch (Exception e) {
                        startActivity(new Intent(
                                Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION));
                    }
                })
                .setNegativeButton(R.string.quit, (d, w) -> finish())
                .show();
    }

    private void maybeAskNotifications() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && ContextCompat.checkSelfPermission(this,
                        Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this,
                    new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIF);
        }
    }

    // ---------- permissions ----------

    private boolean hasStoragePermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            return Environment.isExternalStorageManager();
        }
        return ContextCompat.checkSelfPermission(this,
                Manifest.permission.READ_EXTERNAL_STORAGE)
                == PackageManager.PERMISSION_GRANTED;
    }

    // ---------- helpers ----------

    private void copyToClipboard(String label, String text) {
        if (text == null) return;
        ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        if (cm == null) return;
        cm.setPrimaryClip(ClipData.newPlainText(label, text));
        toast("Copied");
    }

    private void toast(String msg) {
        UiKit.toast(this, msg);
    }
}