package com.defxult.morse;

import android.content.Context;
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.TextView;
import android.widget.Toast;

/**
 * Shared UI helpers: styled toasts so messages look like part of the app
 * instead of Android's default gray rectangle.
 */
public final class UiKit {

    private UiKit() {}

    public static void toast(Context ctx, String msg) {
        try {
            LayoutInflater inflater = LayoutInflater.from(ctx);
            View layout = inflater.inflate(R.layout.toast_custom, null);
            TextView text = layout.findViewById(R.id.toastText);
            text.setText(msg);

            Toast t = new Toast(ctx);
            t.setView(layout);
            t.setDuration(Toast.LENGTH_LONG);
            t.setGravity(Gravity.BOTTOM, 0, (int) (120 * ctx.getResources()
                    .getDisplayMetrics().density));
            t.show();
        } catch (Exception e) {
            // Fallback to the plain system toast if anything fails.
            Toast.makeText(ctx, msg, Toast.LENGTH_LONG).show();
        }
    }
}
