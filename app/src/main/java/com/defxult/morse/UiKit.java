package com.defxult.morse;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.GradientDrawable;
import android.view.Gravity;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;
import android.widget.Toast;

public final class UiKit {

    public static final String TYPE_INFO = "info";
    public static final String TYPE_SUCCESS = "success";
    public static final String TYPE_ERROR = "error";

    private UiKit() {}

    // ==================== Toasts ====================

    public static void toast(Context ctx, String msg) {
        toast(ctx, msg, TYPE_INFO);
    }

    public static void toast(Context ctx, String msg, String type) {
        if (ctx == null) return;
        try {
            LayoutInflater inflater = LayoutInflater.from(ctx);
            View v = inflater.inflate(R.layout.toast_custom, null);
            TextView text = v.findViewById(R.id.toastText);
            text.setText(msg);

            View dot = v.findViewById(R.id.toastDot);
            GradientDrawable d = new GradientDrawable();
            d.setShape(GradientDrawable.OVAL);
            d.setColor(colorFor(ctx, type));
            dot.setBackground(d);

            Toast t = new Toast(ctx);
            t.setView(v);
            t.setDuration(Toast.LENGTH_LONG);
            t.setGravity(Gravity.BOTTOM, 0,
                    (int) (110 * ctx.getResources().getDisplayMetrics().density));
            t.show();
        } catch (Throwable e) {
            Toast.makeText(ctx, msg, Toast.LENGTH_LONG).show();
        }
    }

    private static int colorFor(Context ctx, String type) {
        switch (type == null ? "" : type) {
            case TYPE_SUCCESS: return 0xFF30D158;
            case TYPE_ERROR:   return 0xFFFF453A;
            default:
                try {
                    return Color.parseColor(Prefs.getAccent(ctx));
                } catch (Exception e) {
                    return Color.parseColor(Prefs.DEFAULT_ACCENT);
                }
        }
    }

    // ==================== Dialogs ====================

    public static AlertDialog showDialog(
            Activity activity,
            String title,
            String body,
            String cancelText,
            Runnable onCancel,
            String confirmText,
            Runnable onConfirm) {

        View v = LayoutInflater.from(activity).inflate(R.layout.dialog_styled, null);
        ((TextView) v.findViewById(R.id.dialogTitle)).setText(title);
        ((TextView) v.findViewById(R.id.dialogBody)).setText(body);

        final AlertDialog dialog = new AlertDialog.Builder(activity)
                .setView(v)
                .setCancelable(false)
                .create();
        if (dialog.getWindow() != null) {
            dialog.getWindow().setBackgroundDrawable(new ColorDrawable(Color.TRANSPARENT));
        }

        int accent = ThemeManager.accentColor(activity);

        Button cancel = v.findViewById(R.id.dialogCancel);
        Button confirm = v.findViewById(R.id.dialogConfirm);

        if (cancelText == null || cancelText.isEmpty()) {
            cancel.setVisibility(View.GONE);
        } else {
            cancel.setText(cancelText);
            cancel.setTextColor(accent);
            cancel.setOnClickListener(b -> {
                dialog.dismiss();
                if (onCancel != null) onCancel.run();
            });
        }

        if (confirmText == null || confirmText.isEmpty()) {
            confirm.setVisibility(View.GONE);
        } else {
            confirm.setText(confirmText);
            GradientDrawable bg = new GradientDrawable(
                    GradientDrawable.Orientation.TL_BR,
                    new int[]{ ThemeManager.lighten(accent, 0.18f), accent });
            float r = 13f * activity.getResources().getDisplayMetrics().density;
            bg.setCornerRadius(r);
            confirm.setBackground(bg);
            confirm.setOnClickListener(b -> {
                dialog.dismiss();
                if (onConfirm != null) onConfirm.run();
            });
        }

        dialog.show();
        return dialog;
    }
}
