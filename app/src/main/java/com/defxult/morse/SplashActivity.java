package com.defxult.morse;

import android.animation.Animator;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.ViewAnimationUtils;
import android.view.animation.AccelerateInterpolator;
import android.view.animation.DecelerateInterpolator;
import android.view.animation.OvershootInterpolator;


public class SplashActivity extends BaseActivity {

    private static final long REVEAL_MS = 460;
    private static final long BOUNCE_MS = 600;
    private static final long HOLD_MS = 380;
    private static final long EXIT_MS = 360;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_splash);

        final View reveal = findViewById(R.id.splashReveal);
        final View mark = findViewById(R.id.splashMark);

        mark.setAlpha(0f);
        mark.setScaleX(0.4f);
        mark.setScaleY(0.4f);

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

            new Handler(Looper.getMainLooper()).postDelayed(() -> {
                mark.animate()
                        .alpha(1f)
                        .scaleX(1f).scaleY(1f)
                        .setDuration(BOUNCE_MS)
                        .setInterpolator(new OvershootInterpolator(1.5f))
                        .withEndAction(() -> {
                            // soft float while we hold
                            mark.animate()
                                    .translationY(-10f)
                                    .setDuration(HOLD_MS)
                                    .setInterpolator(new android.view.animation.AccelerateDecelerateInterpolator())
                                    .start();
                        })
                        .start();
            }, REVEAL_MS - 100);

            long exitAt = REVEAL_MS + BOUNCE_MS + HOLD_MS;
            new Handler(Looper.getMainLooper()).postDelayed(() -> {
                float dy = -reveal.getHeight() * 1.1f;
                mark.animate()
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
        // swallow — splash is transient
    }
}
