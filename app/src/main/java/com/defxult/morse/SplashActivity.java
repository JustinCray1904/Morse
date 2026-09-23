package com.defxult.morse;

import android.animation.Animator;
import android.animation.AnimatorListenerAdapter;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.ViewAnimationUtils;
import android.view.animation.AccelerateInterpolator;
import android.view.animation.DecelerateInterpolator;
import android.view.animation.OvershootInterpolator;

import androidx.appcompat.app.AppCompatActivity;

public class SplashActivity extends AppCompatActivity {

    private static final long REVEAL_MS = 480;
    private static final long BOUNCE_MS = 620;
    private static final long HOLD_MS = 420;
    private static final long EXIT_MS = 380;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        ThemeManager.applyNightMode(this);
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_splash);

        final View reveal = findViewById(R.id.splashReveal);
        final View content = findViewById(R.id.splashContent);
        final View mark = findViewById(R.id.splashMark);
        final View text = findViewById(R.id.splashText);

        // Prepare mark + text for rubber bounce-in.
        mark.setAlpha(0f);
        mark.setScaleX(0.35f);
        mark.setScaleY(0.35f);

        text.setAlpha(0f);
        text.setTranslationY(20f);

        // Wait for layout to measure before animating.
        reveal.post(() -> {
            int cx = reveal.getWidth() / 2;
            int cy = reveal.getHeight() / 2;
            float endR = (float) Math.hypot(
                    Math.max(cx, reveal.getWidth() - cx),
                    Math.max(cy, reveal.getHeight() - cy));

            Animator circle = ViewAnimationUtils.createCircularReveal(
                    reveal, cx, cy, 0f, endR);
            circle.setDuration(REVEAL_MS);
            circle.setInterpolator(new DecelerateInterpolator(1.6f));

            reveal.setVisibility(View.VISIBLE);
            circle.start();

            // Bounce the mark in as the reveal finishes.
            new Handler(Looper.getMainLooper()).postDelayed(() -> {
                mark.animate()
                        .alpha(1f)
                        .scaleX(1f).scaleY(1f)
                        .setDuration(BOUNCE_MS)
                        .setInterpolator(new OvershootInterpolator(1.6f))
                        .start();

                text.animate()
                        .alpha(1f)
                        .translationY(0f)
                        .setDuration(BOUNCE_MS)
                        .setStartDelay(140)
                        .setInterpolator(new OvershootInterpolator(1.3f))
                        .start();
            }, REVEAL_MS - 120);

            // Hold, then slide everything up and hand off.
            long exitAt = REVEAL_MS + BOUNCE_MS + HOLD_MS;
            new Handler(Looper.getMainLooper()).postDelayed(() -> {
                float dy = -content.getHeight() * 1.6f - reveal.getHeight() * 0.25f;
                content.animate()
                        .translationY(dy)
                        .alpha(0f)
                        .setDuration(EXIT_MS)
                        .setInterpolator(new AccelerateInterpolator(1.7f))
                        .start();
                reveal.animate()
                        .translationY(dy)
                        .alpha(0f)
                        .setDuration(EXIT_MS)
                        .setInterpolator(new AccelerateInterpolator(1.7f))
                        .withEndAction(() -> {
                            startActivity(new Intent(
                                    SplashActivity.this, MainActivity.class));
                            overridePendingTransition(
                                    R.anim.fade_in, R.anim.fade_out);
                            finish();
                        })
                        .start();
            }, exitAt);
        });
    }

    @Override
    public void onBackPressed() {
        // Swallow — splash is transient.
    }
}
