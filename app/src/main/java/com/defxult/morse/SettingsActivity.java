package com.defxult.morse;

import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.MimeTypeMap;
import android.widget.EditText;
import android.widget.ImageButton;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;

public class SettingsActivity extends AppCompatActivity {

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

    private String selectedAccent;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        ThemeManager.applyNightMode(this);
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);

        themeGroup = findViewById(R.id.themeGroup);
        accentRow = findViewById(R.id.accentRow);
        portField = findViewById(R.id.portField);
        portHint = findViewById(R.id.portHint);
        uploadDestRow = findViewById(R.id.uploadDestRow);
        uploadDestValue = findViewById(R.id.uploadDestValue);
        btnBack = findViewById(R.id.btnBack);

        btnBack.setOnClickListener(v -> {
            commitPort();
            finish();
        });

        setupTheme();
        setupAccent();
        setupPort();
        setupUploadDest();
        setupMediaButtons();

        TextView aboutVersion = findViewById(R.id.aboutVersion);
        aboutVersion.setText(getString(R.string.about_version, "1.0"));
    }

    // ---------- theme ----------

    private void setupTheme() {
        String t = Prefs.getTheme(this);
        RadioButton rb;
        if (Prefs.THEME_LIGHT.equals(t)) rb = findViewById(R.id.themeLight);
        else if (Prefs.THEME_DARK.equals(t)) rb = findViewById(R.id.themeDark);
        else rb = findViewById(R.id.themeSystem);
        rb.setChecked(true);

        themeGroup.setOnCheckedChangeListener((group, checkedId) -> {
            String next;
            if (checkedId == R.id.themeLight) next = Prefs.THEME_LIGHT;
            else if (checkedId == R.id.themeDark) next = Prefs.THEME_DARK;
            else next = Prefs.THEME_SYSTEM;
            Prefs.setTheme(this, next);
            // AppCompatDelegate triggers recreate on the whole task when the
            // mode actually changes; nothing else to do here.
        });
    }

    // ---------- accent ----------

    private void setupAccent() {
        selectedAccent = Prefs.getAccent(this);
        accentRow.removeAllViews();
        float density = getResources().getDisplayMetrics().density;
        int size = (int) (44 * density);
        int margin = (int) (8 * density);

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
                // Refresh all swatches to move the selection ring.
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

    // ---------- port ----------

    private void setupPort() {
        boolean running = Prefs.isRunning(this);
        int port = Prefs.getPort(this);
        portField.setText(String.valueOf(port));
        portField.setInputType(InputType.TYPE_CLASS_NUMBER);

        if (running) {
            portField.setEnabled(false);
            portHint.setVisibility(View.VISIBLE);
            portHint.setText(R.string.port_locked);
        } else {
            portField.setEnabled(true);
            portHint.setVisibility(View.GONE);
        }

        portField.setOnFocusChangeListener((v, hasFocus) -> {
            if (!hasFocus) commitPort();
        });
    }

    @Override
    protected void onPause() {
        commitPort();
        super.onPause();
    }

    @Override
    public void onBackPressed() {
        commitPort();
        super.onBackPressed();
    }

    private void commitPort() {
        if (!portField.isEnabled()) return;
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

    // ---------- upload destination ----------

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

    // ---------- media picks ----------

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

        // Delete previous variants.
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

    // ---------- helpers ----------

    private void toast(String msg) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show();
    }
}