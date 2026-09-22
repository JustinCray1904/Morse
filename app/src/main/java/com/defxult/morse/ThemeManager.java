package com.defxult.morse;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;

import androidx.appcompat.app.AppCompatDelegate;

/**
 * Reads theme + accent from Prefs, applies them.
 *
 * applyNightMode() must be called before super.onCreate() in every Activity.
 * applyAccentTo*() are called after setContentView() with the views to tint.
 */
public final class ThemeManager {

    /** Palette offered in Settings. First entry is the default. */
    public static final String[] ACCENT_PALETTE = new String[] {
        "#64d2ff", // cyan (default)
        "#0a84ff", // blue
        "#bf5af2", // purple
        "#30d158", // green
        "#ff9f0a", // orange
        "#ff453a", // red
        "#ff375f", // pink
        "#5ac8fa", // sky
    };

    private ThemeManager() {}

    // ---------- night mode ----------

    /** Call in onCreate() before super.onCreate(). */
    public static void applyNightMode(Activity activity) {
        String theme = Prefs.getTheme(activity);
        int mode;
        switch (theme) {
            case Prefs.THEME_LIGHT: mode = AppCompatDelegate.MODE_NIGHT_NO; break;
            case Prefs.THEME_DARK:  mode = AppCompatDelegate.MODE_NIGHT_YES; break;
            default:                mode = AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM; break;
        }
        AppCompatDelegate.setDefaultNightMode(mode);
    }

    // ---------- accent helpers ----------

    public static int accentColor(Activity activity) {
        try {
            return Color.parseColor(Prefs.getAccent(activity));
        } catch (Exception e) {
            return Color.parseColor(Prefs.DEFAULT_ACCENT);
        }
    }

    public static int lighten(int color, float amt) {
        int r = Color.red(color);
        int g = Color.green(color);
        int b = Color.blue(color);
        if (amt >= 0) {
            r = (int) (r + (255 - r) * amt);
            g = (int) (g + (255 - g) * amt);
            b = (int) (b + (255 - b) * amt);
        } else {
            float f = 1f + amt;
            r = (int) (r * f);
            g = (int) (g * f);
            b = (int) (b * f);
        }
        return Color.argb(
                Color.alpha(color),
                Math.max(0, Math.min(255, r)),
                Math.max(0, Math.min(255, g)),
                Math.max(0, Math.min(255, b)));
    }

    private static float dp(View v, float n) {
        return n * v.getResources().getDisplayMetrics().density;
    }

    // ---------- button styling ----------

    /** Filled, gradient, accent. */
    public static void stylePrimaryButton(Button btn, int accent) {
        GradientDrawable g = new GradientDrawable(
                GradientDrawable.Orientation.TL_BR,
                new int[]{ lighten(accent, 0.10f), accent });
        g.setCornerRadius(dp(btn, 12f));
        btn.setBackground(g);
        btn.setTextColor(Color.WHITE);
        btn.setAllCaps(false);
        btn.setStateListAnimator(null);
        btn.setElevation(dp(btn, 3f));
    }

    /** Outlined, transparent, accent text. */
    public static void styleSecondaryButton(Button btn, int accent) {
        GradientDrawable g = new GradientDrawable();
        g.setCornerRadius(dp(btn, 12f));
        g.setColor(Color.TRANSPARENT);
        g.setStroke((int) dp(btn, 1.5f), accent);
        btn.setBackground(g);
        btn.setTextColor(accent);
        btn.setAllCaps(false);
        btn.setStateListAnimator(null);
        btn.setElevation(0f);
    }

    /** Filled red, ignores accent. For Stop. */
    public static void styleDangerButton(Button btn) {
        int red = Color.parseColor("#ff453a");
        GradientDrawable g = new GradientDrawable(
                GradientDrawable.Orientation.TL_BR,
                new int[]{ lighten(red, 0.10f), red });
        g.setCornerRadius(dp(btn, 12f));
        btn.setBackground(g);
        btn.setTextColor(Color.WHITE);
        btn.setAllCaps(false);
        btn.setStateListAnimator(null);
        btn.setElevation(dp(btn, 3f));
    }

    // ---------- misc tinting ----------

    public static void tintText(TextView tv, int color) {
        tv.setTextColor(color);
    }

    /** Small filled circle (status dot). */
    public static void tintDot(View dot, int accent) {
        GradientDrawable g = new GradientDrawable();
        g.setShape(GradientDrawable.OVAL);
        g.setColor(accent);
        dot.setBackground(g);
    }

    /** Rounded rect outline, e.g. for the QR code border. */
    public static void tintBorder(View v, int accent, float strokeDp) {
        GradientDrawable g = new GradientDrawable();
        g.setCornerRadius(dp(v, 8f));
        g.setColor(Color.WHITE);
        g.setStroke((int) dp(v, strokeDp), accent);
        v.setBackground(g);
    }

    /** Swatch for the settings palette. Caller passes a View. */
    public static void tintSwatch(View swatch, int color, boolean selected) {
        GradientDrawable g = new GradientDrawable();
        g.setShape(GradientDrawable.OVAL);
        g.setColor(color);
        if (selected) {
            g.setStroke((int) dp(swatch, 3f), Color.WHITE);
        }
        swatch.setBackground(g);
    }
}