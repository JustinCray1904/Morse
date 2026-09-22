package com.defxult.morse;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * Thin wrapper around SharedPreferences. Keys, defaults, getters, setters.
 * No business logic.
 */
public final class Prefs {

    private static final String FILE = "morse_prefs";

    public static final String KEY_THEME       = "theme";        // "system" | "light" | "dark"
    public static final String KEY_ACCENT      = "accent";       // "#rrggbb"
    public static final String KEY_PORT        = "port";         // int
    public static final String KEY_RUNNING     = "running";      // bool
    public static final String KEY_UPLOAD_DEST = "upload_dest";  // relative path, "" = unset

    public static final String THEME_SYSTEM = "system";
    public static final String THEME_LIGHT  = "light";
    public static final String THEME_DARK   = "dark";

    public static final String DEFAULT_ACCENT      = "#64d2ff";
    public static final int    DEFAULT_PORT        = 8080;
    public static final int    MIN_PORT            = 1024;
    public static final int    MAX_PORT            = 65535;
    public static final String DEFAULT_UPLOAD_DEST = "";

    private Prefs() {}

    private static SharedPreferences sp(Context ctx) {
        return ctx.getApplicationContext().getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    // ---------- theme ----------

    public static String getTheme(Context ctx) {
        return sp(ctx).getString(KEY_THEME, THEME_SYSTEM);
    }

    public static void setTheme(Context ctx, String theme) {
        if (!THEME_SYSTEM.equals(theme) && !THEME_LIGHT.equals(theme) && !THEME_DARK.equals(theme)) {
            theme = THEME_SYSTEM;
        }
        sp(ctx).edit().putString(KEY_THEME, theme).apply();
    }

    // ---------- accent ----------

    public static String getAccent(Context ctx) {
        return sp(ctx).getString(KEY_ACCENT, DEFAULT_ACCENT);
    }

    public static void setAccent(Context ctx, String hex) {
        if (hex == null || !hex.matches("^#[0-9a-fA-F]{6}$")) {
            hex = DEFAULT_ACCENT;
        }
        sp(ctx).edit().putString(KEY_ACCENT, hex).apply();
    }

    // ---------- port ----------

    public static int getPort(Context ctx) {
        int p = sp(ctx).getInt(KEY_PORT, DEFAULT_PORT);
        if (p < MIN_PORT || p > MAX_PORT) p = DEFAULT_PORT;
        return p;
    }

    public static void setPort(Context ctx, int port) {
        if (port < MIN_PORT || port > MAX_PORT) port = DEFAULT_PORT;
        sp(ctx).edit().putInt(KEY_PORT, port).apply();
    }

    // ---------- running flag ----------

    public static boolean isRunning(Context ctx) {
        return sp(ctx).getBoolean(KEY_RUNNING, false);
    }

    public static void setRunning(Context ctx, boolean running) {
        sp(ctx).edit().putBoolean(KEY_RUNNING, running).apply();
    }

    // ---------- upload destination ----------

    public static String getUploadDest(Context ctx) {
        String p = sp(ctx).getString(KEY_UPLOAD_DEST, DEFAULT_UPLOAD_DEST);
        if (p == null) return DEFAULT_UPLOAD_DEST;
        return p.replaceAll("^/+|/+$", "");
    }

    public static void setUploadDest(Context ctx, String relative) {
        if (relative == null) relative = DEFAULT_UPLOAD_DEST;
        relative = relative.replaceAll("^/+|/+$", "");
        sp(ctx).edit().putString(KEY_UPLOAD_DEST, relative).apply();
    }
}