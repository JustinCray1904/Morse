package com.defxult.morse;

import android.content.Context;
import android.content.res.Configuration;

import androidx.appcompat.app.AppCompatActivity;

/**
 * Every activity extends this. attachBaseContext runs before onCreate and
 * before any view is inflated, so forcing the uiMode here guarantees the
 * whole activity — every layout, every color — is inflated with the theme
 * the user picked, regardless of what AppCompatDelegate happens to think.
 */
public abstract class BaseActivity extends AppCompatActivity {

    @Override
    protected void attachBaseContext(Context newBase) {
        Configuration config =
                new Configuration(newBase.getResources().getConfiguration());

        String theme = Prefs.getTheme(newBase);
        int nightBit;
        switch (theme) {
            case Prefs.THEME_LIGHT:
                nightBit = Configuration.UI_MODE_NIGHT_NO;
                break;
            case Prefs.THEME_DARK:
                nightBit = Configuration.UI_MODE_NIGHT_YES;
                break;
            default: {
                // Follow system: keep whatever the phone reports.
                int sys = config.uiMode & Configuration.UI_MODE_NIGHT_MASK;
                nightBit = (sys == Configuration.UI_MODE_NIGHT_YES)
                        ? Configuration.UI_MODE_NIGHT_YES
                        : Configuration.UI_MODE_NIGHT_NO;
                break;
            }
        }
        config.uiMode = (config.uiMode & ~Configuration.UI_MODE_NIGHT_MASK) | nightBit;

        Context wrapped = newBase.createConfigurationContext(config);
        super.attachBaseContext(wrapped);
    }
}
