package com.defxult.morse;

import android.app.Activity;
import android.app.AlertDialog;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.graphics.drawable.GradientDrawable;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.Button;
import android.widget.TextView;

/**
 * Shared styled dialogs. Replaces AlertDialog's default flat look with the
 * app's glass surface and accent gradient buttons.
 */
public final class UiKit {

    private UiKit() {}

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
            bg.setStroke((int) (1f * activity.getResources().getDisplayMetrics().density),
                    0x40FFFFFF);
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
