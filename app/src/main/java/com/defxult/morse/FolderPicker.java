package com.defxult.morse;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Environment;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.ListView;
import android.widget.TextView;

import java.io.File;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * Simple folder picker scoped to external storage. Returns a path relative
 * to Environment.getExternalStorageDirectory() ("Download", "DCIM/Camera",
 * or "" for the root).
 */
public final class FolderPicker {

    public interface OnPicked {
        void onPicked(String relativePath);
    }

    private FolderPicker() {}

    public static void show(Activity activity, String initialRelative, final OnPicked onPicked) {
        final File root = Environment.getExternalStorageDirectory();
        final String[] current = { initialRelative == null ? "" : initialRelative };

        View view = LayoutInflater.from(activity).inflate(R.layout.dialog_folder_picker, null);
        final TextView pathLabel = view.findViewById(R.id.pickerPath);
        final ListView list = view.findViewById(R.id.pickerList);
        final Button upBtn = view.findViewById(R.id.pickerUp);
        final Button selectBtn = view.findViewById(R.id.pickerSelect);

        final AlertDialog dialog = new AlertDialog.Builder(activity)
                .setView(view)
                .setNegativeButton(android.R.string.cancel, null)
                .create();

        final Runnable[] refresh = new Runnable[1];

        refresh[0] = () -> {
            File dir = current[0].isEmpty() ? root : new File(root, current[0]);
            if (!dir.isDirectory()) dir = root;

            pathLabel.setText(current[0].isEmpty() ? "/sdcard" : "/sdcard/" + current[0]);
            upBtn.setEnabled(!current[0].isEmpty());

            File[] children = dir.listFiles(File::isDirectory);
            List<String> labels = new ArrayList<>();
            final List<String> names = new ArrayList<>();
            if (children != null) {
                Arrays.sort(children, (a, b) ->
                        a.getName().compareToIgnoreCase(b.getName()));
                for (File c : children) {
                    if (c.getName().startsWith(".")) continue;
                    labels.add("\uD83D\uDCC1  " + c.getName());
                    names.add(c.getName());
                }
            }
            if (labels.isEmpty()) labels.add("(no subfolders)");

            ArrayAdapter<String> adapter = new ArrayAdapter<>(
                    activity, android.R.layout.simple_list_item_1, labels);
            list.setAdapter(adapter);

            list.setOnItemClickListener((parent, v, position, id) -> {
                if (position >= names.size()) return;
                String name = names.get(position);
                current[0] = current[0].isEmpty() ? name : current[0] + "/" + name;
                refresh[0].run();
            });
        };

        upBtn.setOnClickListener(v -> {
            if (current[0].isEmpty()) return;
            int slash = current[0].lastIndexOf('/');
            current[0] = (slash < 0) ? "" : current[0].substring(0, slash);
            refresh[0].run();
        });

        selectBtn.setOnClickListener(v -> {
            dialog.dismiss();
            onPicked.onPicked(current[0]);
        });

        refresh[0].run();
        dialog.show();
    }
}