package com.defxult.morse;

import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;

import androidx.appcompat.app.AppCompatActivity;

public class SplashActivity extends AppCompatActivity {
    private static final long DELAY_MS = 1200;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        ThemeManager.applyNightMode(this);
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_splash);

        View mark = findViewById(R.id.splashMark);
        View text = findViewById(R.id.splashText);

        mark.setAlpha(0f);
        mark.setScaleX(0.85f);
        mark.setScaleY(0.85f);
        mark.animate()
                .alpha(1f).scaleX(1f).scaleY(1f)
                .setDuration(650)
                .start();

        text.setAlpha(0f);
        text.setTranslationY(20f);
        text.animate()
                .alpha(1f).translationY(0f)
                .setDuration(650).setStartDelay(220)
                .start();

        new Handler(Looper.getMainLooper()).postDelayed(() -> {
            startActivity(new Intent(this, MainActivity.class));
            overridePendingTransition(R.anim.fade_in, R.anim.fade_out);
            finish();
        }, DELAY_MS);
    }
}
