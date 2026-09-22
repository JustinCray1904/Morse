package com.defxult.morse;

import android.content.Context;
import android.content.Intent;
import android.provider.Settings;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

/**
 * Determines whether a usable (non-loopback) IP is available, by asking the
 * Python side. Also provides the best-available intent to the system hotspot
 * settings screen.
 *
 * Requires Python.start() to have been called already.
 */
public final class NetworkCheck {

    public static final class Result {
        public final boolean ok;
        public final String ip;

        Result(boolean ok, String ip) {
            this.ok = ok;
            this.ip = ip;
        }
    }

    private NetworkCheck() {}

    public static Result check() {
        try {
            Python py = Python.getInstance();
            PyObject mod = py.getModule("defxult_transfer");
            PyObject ipObj = mod.callAttr("get_local_ip");
            String ip = (ipObj == null) ? null : ipObj.toJava(String.class);
            if (ip == null || ip.isEmpty() || "127.0.0.1".equals(ip)) {
                return new Result(false, null);
            }
            return new Result(true, ip);
        } catch (Exception e) {
            return new Result(false, null);
        }
    }

    /**
     * Best-effort intent to the system hotspot / tethering settings screen.
     * Different OEMs expose different activities; we try a few in order.
     */
    public static Intent hotspotSettingsIntent(Context ctx) {
        // "android.settings.TETHER_SETTINGS" is not a public constant but is
        // the actual action string on stock Android and most OEMs.
        Intent[] candidates = new Intent[] {
            new Intent().setAction("android.settings.TETHER_SETTINGS"),
            new Intent(Settings.ACTION_WIRELESS_SETTINGS),
            new Intent(Settings.ACTION_WIFI_SETTINGS),
        };
        for (Intent i : candidates) {
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            if (i.resolveActivity(ctx.getPackageManager()) != null) {
                return i;
            }
        }
        Intent i = new Intent(Settings.ACTION_SETTINGS);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return i;
    }
}