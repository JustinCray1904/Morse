package com.defxult.morse;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import androidx.annotation.Nullable;
import androidx.core.app.NotificationCompat;

/**
 * Foreground service that keeps the app process alive and holds a partial
 * wake lock while the Python server is running.
 *
 * Lifecycle notes:
 *   - Manifest sets stopWithTask="true", so swiping the app from recents
 *     calls onTaskRemoved, and Android tears down the service automatically.
 *   - onTaskRemoved also explicitly stops the Python server, in case the
 *     process is kept alive briefly by a lingering thread.
 *   - The notification's Stop action sends ACTION_STOP back to us.
 */
public class ServerService extends Service {

    private static final String TAG = "morse";
    private static final String CHANNEL_ID = "morse_server";
    private static final int NOTIF_ID = 42;

    public static final String ACTION_STOP = "com.defxult.morse.STOP";
    private static final String EXTRA_PORT = "port";

    private PowerManager.WakeLock wakeLock;

    // ---------- static helpers ----------

    public static void start(Context ctx, int port) {
        Intent i = new Intent(ctx, ServerService.class);
        i.putExtra(EXTRA_PORT, port);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            ctx.startForegroundService(i);
        } else {
            ctx.startService(i);
        }
    }

    public static void stop(Context ctx) {
        ctx.stopService(new Intent(ctx, ServerService.class));
    }

    // ---------- lifecycle ----------

    @Override
    public void onCreate() {
        super.onCreate();
        createChannel();

        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        if (pm != null) {
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "morse:server");
            wakeLock.setReferenceCounted(false);
            wakeLock.acquire();
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }

        int port = (intent != null) ? intent.getIntExtra(EXTRA_PORT, 8080) : 8080;
        try {
            startForeground(NOTIF_ID, buildNotification(port));
            android.util.Log.i(TAG, "foreground service started on port " + port);
        } catch (Exception e) {
            android.util.Log.e(TAG, "startForeground failed", e);
            stopSelf();
            return START_NOT_STICKY;
        }
        return START_STICKY;
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        // Swipe from recents. Kill the server and the service explicitly,
        // in addition to what the manifest's stopWithTask does.
        ServerController.stop(this);
        stopForeground(true);
        stopSelf();
        super.onTaskRemoved(rootIntent);
    }

    @Override
    public void onDestroy() {
        if (wakeLock != null && wakeLock.isHeld()) {
            try { wakeLock.release(); } catch (Exception ignored) {}
            wakeLock = null;
        }
        // Defensive: if we got here without anyone calling stop(), clean up.
        ServerController.stop(this);
        super.onDestroy();
    }

    @Nullable
    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ---------- notification ----------

    private Notification buildNotification(int port) {
        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);

        int piFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            piFlags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent contentPi = PendingIntent.getActivity(this, 0, open, piFlags);

        Intent stopIntent = new Intent(this, ServerService.class);
        stopIntent.setAction(ACTION_STOP);
        PendingIntent stopPi = PendingIntent.getService(this, 1, stopIntent, piFlags);

        return new NotificationCompat.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_sys_upload)
                .setContentTitle(getString(R.string.notif_title))
                .setContentText(getString(R.string.notif_text, port))
                .setOngoing(true)
                .setContentIntent(contentPi)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .setCategory(NotificationCompat.CATEGORY_SERVICE)
                .addAction(0, getString(R.string.stop_server), stopPi)
                .build();
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm == null) return;
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL_ID,
                    getString(R.string.notif_channel_name),
                    NotificationManager.IMPORTANCE_LOW);
            ch.setDescription(getString(R.string.notif_channel_desc));
            ch.setShowBadge(false);
            nm.createNotificationChannel(ch);
        }
    }
}