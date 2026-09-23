package com.defxult.morse;

import android.app.PendingIntent;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.util.Log;
import android.view.View;
import android.webkit.MimeTypeMap;
import android.widget.EditText;
import android.widget.ImageButton;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatDelegate;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;

public class SettingsActivity extends BaseActivity {

    private static final String TAG = "morse";
    private static final int REQ_WALLPAPER = 2001;
    private static final int REQ_MUSIC = 2002;

    private RadioGroup themeGroup;
    private LinearLayout accentRow;
    private EditText portField;
    private TextView portHint;
    private LinearLayout uploadDestRow;
    private TextView uploadDestValue;
    private ImageButton btnBack;
    private TextView headerAppearance, headerServer, headerCustomization, headerAbout;

    private String selectedAccent;
    private int currentAccentColor;
    private boolean suppressThemeListener = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);

        themeGroup = findViewById(R.id.themeGroup);
        accentRow = findViewById(R.id.accentRow);
        portField = findViewById(R.id.portField);
        portHint = findViewById(R.id.portHint);
        uploadDestRow = findViewById(R.id.uploadDestRow);
        uploadDestValue = findViewById(R.id.uploadDestValue);
        btnBack = findViewById(R.id.btnBack);
        headerAppearance = findViewById(R.id.headerAppearance);
        headerServer = findViewById(R.id.headerServer);
        headerCustomization = findViewById(R.id.headerCustomization);
        headerAbout = findViewById(R.id.headerAbout);

        btnBack.setOnClickListener(v -> {
            commitPort();
            finish();
        });

        currentAccentColor = ThemeManager.accentColor(this);

        setupTheme();
        setupAccent();
        setupPort();
        setupUploadDest();
        setupMediaButtons();

        TextView aboutVersion = findViewById(R.id.aboutVersion);
        aboutVersion.setText(getBuildVersion());
    }

    private String getBuildVersion() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "1.0";
        }
    }

    // ==================== THEME ====================

    private void setupTheme() {
        String t = Prefs.getTheme(this);
        RadioButton rb;
        if (Prefs.THEME_LIGHT.equals(t)) rb = findViewById(R.id.themeLight);
        else if (Prefs.THEME_DARK.equals(t)) rb = findViewById(R.id.themeDark);
        else rb = findViewById(R.id.themeSystem);

        suppressThemeListener = true;
        rb.setChecked(true);
        suppressThemeListener = false;

        themeGroup.setOnCheckedChangeListener((group, checkedId) -> {
            if (suppressThemeListener) return;
            String next;
            if (checkedId == R.id.themeLight) next = Prefs.THEME_LIGHT;
            else if (checkedId == R.id.themeDark) next = Prefs.THEME_DARK;
            else next = Prefs.THEME_SYSTEM;

            if (next.equals(Prefs.getTheme(this))) return;

            Prefs.setTheme(this, next);

            int mode;
            if (Prefs.THEME_LIGHT.equals(next)) mode = AppCompatDelegate.MODE_NIGHT_NO;
            else if (Prefs.THEME_DARK.equals(next)) mode = AppCompatDelegate.MODE_NIGHT_YES;
            else mode = AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM;
            AppCompatDelegate.setDefaultNightMode(mode);

            restartAppFully();
        });
    }

    private void restartAppFully() {
        try {
            Intent launch = getPackageManager().getLaunchIntentForPackage(getPackageName());
            if (launch != null) {
                launch.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP
                        | Intent.FLAG_ACTIVITY_NEW_TASK
                        | Intent.FLAG_ACTIVITY_CLEAR_TASK);
                startActivity(launch);
            }
            finishAffinity();
        } catch (Exception e) {
            Log.e(TAG, "restart failed", e);
        }
    }

    // ==================== ACCENT ====================

    private void setupAccent() {
        selectedAccent = Prefs.getAccent(this);
        accentRow.removeAllViews();
        float density = getResources().getDisplayMetrics().density;
        int size = (int) (38 * density);
        int margin = (int) (6 * density);

        for (String hex : ThemeManager.ACCENT_PALETTE) {
            ImageView swatch = new ImageView(this);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(size, size);
            lp.setMargins(margin, margin, margin, margin);
            swatch.setLayoutParams(lp);
            swatch.setContentDescription(hex);

            int color;
            try { color = Color.parseColor(hex); } catch (Exception e) { color = Color.GRAY; }
            boolean sel = hex.equalsIgnoreCase(selectedAccent);
            ThemeManager.tintSwatch(swatch, color, sel);

            swatch.setOnClickListener(v -> {
                selectedAccent = hex;
                Prefs.setAccent(SettingsActivity.this, hex);
                applyAccentInstant();
                for (int i = 0; i < accentRow.getChildCount(); i++) {
                    View child = accentRow.getChildAt(i);
                    String childHex = (String) child.getContentDescription();
                    int c;
                    try { c = Color.parseColor(childHex); } catch (Exception e) { c = Color.GRAY; }
                    ThemeManager.tintSwatch(child, c, childHex.equalsIgnoreCase(hex));
                }
            });

            accentRow.addView(swatch);
        }
    }

    private void applyAccentInstant() {
        currentAccentColor = ThemeManager.accentColor(this);

        tintHeader(headerAppearance);
        tintHeader(headerServer);
        tintHeader(headerCustomization);
        tintHeader(headerAbout);

        tintRadio(findViewById(R.id.themeSystem));
        tintRadio(findViewById(R.id.themeLight));
        tintRadio(findViewById(R.id.themeDark));

        if (btnBack != null) btnBack.setColorFilter(currentAccentColor);
    }

    private void tintHeader(TextView tv) {
        if (tv != null) tv.setTextColor(currentAccentColor);
    }

    private void tintRadio(RadioButton rb) {
        if (rb == null) return;
        try {
            rb.setButtonTintList(android.content.res.ColorStateList.valueOf(currentAccentColor));
        } catch (Exception ignored) {}
    }

    @Override
    protected void onResume() {
        super.onResume();
        applyAccentInstant();
    }

    // ==================== PORT ====================

    private void setupPort() {
        boolean running = Prefs.isRunning(this);
        int port = Prefs.getPort(this);
        portField.setText(String.valueOf(port));
        portField.setInputType(InputType.TYPE_CLASS_NUMBER);

        if (running) {
            portField.setEnabled(false);
            portField.setTextColor(getResources().getColor(R.color.text_dim));
            portHint.setVisibility(View.VISIBLE);
        } else {
            portField.setEnabled(true);
            portField.setTextColor(getResources().getColor(R.color.text_muted));
            portHint.setVisibility(View.GONE);
        }

        portField.setOnFocusChangeListener((v, hasFocus) -> {
            if (!hasFocus) commitPort();
        });
    }

    @Override
    public void onBackPressed() {
        commitPort();
        super.onBackPressed();
    }

    @Override
    protected void onPause() {
        commitPort();
        super.onPause();
    }

    private void commitPort() {
        if (portField == null || !portField.isEnabled()) return;
        String raw = portField.getText().toString().trim();
        if (raw.isEmpty()) {
            portField.setText(String.valueOf(Prefs.DEFAULT_PORT));
            Prefs.setPort(this, Prefs.DEFAULT_PORT);
            return;
        }
        int p;
        try { p = Integer.parseInt(raw); } catch (Exception e) { p = -1; }
        if (p < Prefs.MIN_PORT || p > Prefs.MAX_PORT) {
            toast(getString(R.string.port_invalid, Prefs.MIN_PORT, Prefs.MAX_PORT));
            portField.setText(String.valueOf(Prefs.getPort(this)));
            return;
        }
        Prefs.setPort(this, p);
    }

    // ==================== UPLOAD DEST ====================

    private void setupUploadDest() {
        refreshUploadDestLabel();
        uploadDestRow.setOnClickListener(v -> {
            String initial = Prefs.getUploadDest(this);
            FolderPicker.show(this, initial, relative -> {
                Prefs.setUploadDest(this, relative);
                refreshUploadDestLabel();
                toast(relative.isEmpty()
                        ? getString(R.string.upload_dest_unset)
                        : "/sdcard/" + relative);
            });
        });
    }

    private void refreshUploadDestLabel() {
        String dest = Prefs.getUploadDest(this);
        if (dest.isEmpty()) {
            uploadDestValue.setText(R.string.upload_dest_unset);
        } else {
            uploadDestValue.setText("/sdcard/" + dest);
        }
    }

    // ==================== MEDIA ====================

    private void setupMediaButtons() {
        findViewById(R.id.btnWallpaper).setOnClickListener(v -> {
            Intent i = new Intent(Intent.ACTION_GET_CONTENT);
            i.setType("image/*");
            i.addCategory(Intent.CATEGORY_OPENABLE);
            try { startActivityForResult(i, REQ_WALLPAPER); }
            catch (Exception e) { toast("No file picker available"); }
        });

        findViewById(R.id.btnMusic).setOnClickListener(v -> {
            Intent i = new Intent(Intent.ACTION_GET_CONTENT);
            i.setType("audio/*");
            i.addCategory(Intent.CATEGORY_OPENABLE);
            try { startActivityForResult(i, REQ_MUSIC); }
            catch (Exception e) { toast("No file picker available"); }
        });
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (res != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();
        if (req == REQ_WALLPAPER) savePicked(uri, ".defxult_wallpaper", true);
        else if (req == REQ_MUSIC) savePicked(uri, ".defxult_music", false);
    }

    private void savePicked(Uri uri, String basename, boolean isImage) {
        String mime = getContentResolver().getType(uri);
        String ext = (mime != null)
                ? MimeTypeMap.getSingleton().getExtensionFromMimeType(mime)
                : null;
        if (ext == null) ext = isImage ? "jpg" : "mp3";

        File root = Environment.getExternalStorageDirectory();
        String[] knownExts = isImage
                ? new String[]{"jpg","jpeg","png","webp","gif","bmp"}
                : new String[]{"mp3","ogg","m4a","wav","flac","opus","aac"};

        for (String e : knownExts) {
            File f = new File(root, basename + "." + e);
            if (f.exists()) f.delete();
        }

        File target = new File(root, basename + "." + ext);
        try (InputStream in = getContentResolver().openInputStream(uri);
             FileOutputStream out = new FileOutputStream(target)) {
            if (in == null) { toast("Cannot read file"); return; }
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            toast("Saved " + target.getName());
            notifyPythonRescan();
        } catch (Exception e) {
            Log.e(TAG, "save failed", e);
            toast("Save failed: " + e.getMessage());
        }
    }

    private void notifyPythonRescan() {
        try {
            if (!Python.isStarted()) return;
            Python py = Python.getInstance();
            PyObject mod = py.getModule("defxult_transfer");
            mod.callAttr("rescan_media");
        } catch (Exception e) {
            Log.w(TAG, "rescan failed", e);
        }
    }

    // ==================== HELPERS ====================

    private void toast(String msg) {
        UiKit.toast(this, msg);
    }
}
