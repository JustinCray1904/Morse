package com.defxult.morse;

import android.app.Activity;
import android.graphics.drawable.GradientDrawable;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.FrameLayout;
import android.widget.ImageButton;
import android.widget.TextView;

/**
 * View-based toast system. Renders inside the activity's own window so we get
 * full control over position, animation, dismiss, and stacking — none of which
 * the platform Toast gives us.
 *
 * Requires an R.id.toastHost FrameLayout in the activity layout, bottom-anchored.
 */
public final class UiKit {

    public static final String TYPE_INFO = "info";
    public static final String TYPE_SUCCESS = "success";
    public static final String TYPE_ERROR = "error";

    private static final long AUTO_DISMISS_MS = 4200;

    private UiKit() {}

    public static void toast(Activity activity, String msg) {
        toast(activity, msg, TYPE_INFO);
    }

    public static void toast(Activity activity, String msg, String type) {
        if (activity == null || activity.isFinishing()) return;
        FrameLayout host = activity.findViewById(R.id.toastHost);
        if (host == null) return;

        View view = LayoutInflater.from(activity)
                .inflate(R.layout.toast_custom, host, false);

        TextView text = view.findViewById(R.id.toastText);
        text.setText(msg);

        View dot = view.findViewById(R.id.toastDot);
        GradientDrawable d = new GradientDrawable();
        d.setShape(GradientDrawable.OVAL);
        d.setColor(colorFor(activity, type));
        dot.setBackground(d);

        ImageButton close = view.findViewById(R.id.toastClose);
        final boolean[] dismissed = { false };
        Runnable dismiss = () -> {
            if (dismissed[0]) return;
            dismissed[0] = true;
            float dY = dp(activity, 28);
            view.animate()
                    .alpha(0f)
                    .translationY(dY)
                    .setDuration(200)
                    .withEndAction(() -> {
                        try { host.removeView(view); } catch (Exception ignored) {}
                    })
                    .start();
        };

        close.setOnClickListener(v -> dismiss.run());

        host.addView(view);

        float fromY = dp(activity, 32);
        view.setAlpha(0f);
        view.setTranslationY(fromY);
        view.animate()
                .alpha(1f)
                .translationY(0f)
                .setDuration(280)
                .start();

        view.postDelayed(dismiss, AUTO_DISMISS_MS);
    }

    private static int colorFor(Activity a, String type) {
        switch (type == null ? "" : type) {
            case TYPE_SUCCESS: return 0xFF30D158;
            case TYPE_ERROR:   return 0xFFFF453A;
            default:           return ThemeManager.accentColor(a);
        }
    }

    private static float dp(Activity a, float n) {
        return n * a.getResources().getDisplayMetrics().density;
    }
}
