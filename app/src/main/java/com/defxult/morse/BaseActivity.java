package com.defxult.morse;

import android.content.Context;
import android.content.res.Configuration;

import androidx.appcompat.app.AppCompatActivity;

/**
 * attachBaseContext runs before onCreate and before any layout is inflated.
 * Overriding the Configuration's night mode here guarantees the entire
 * activity — every @color, every drawable — resolves against the theme the
 * user actually picked, on cold start AND after a recreate().
 */
public abstract class BaseActivity extends AppCompatActivity {

    @Override
    protected void attachBaseContext(Context base) {
        Configuration cfg = new Configuration(base.getResources().getConfiguration());
        String theme = Prefs.getTheme(base);

        int want;
        if (Prefs.THEME_LIGHT.equals(theme)) {
            want = Configuration.UI_MODE_NIGHT_NO;
        } else if (Prefs.THEME_DARK.equals(theme)) {
            want = Configuration.UI_MODE_NIGHT_YES;
        } else {
            int sys = cfg.uiMode & Configuration.UI_MODE_NIGHT_MASK;
            want = (sys == Configuration.UI_MODE_NIGHT_YES)
                    ? Configuration.UI_MODE_NIGHT_YES
                    : Configuration.UI_MODE_NIGHT_NO;
        }
        cfg.uiMode = (cfg.uiMode & ~Configuration.UI_MODE_NIGHT_MASK) | want;

        super.attachBaseContext(base.createConfigurationContext(cfg));
    }
}
