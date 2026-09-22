package com.defxult.morse;

import android.content.Context;
import android.os.Environment;
import android.util.Log;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.List;

/**
 * Owns the Python server's lifecycle:
 *   - asset extraction on first launch / upgrade
 *   - env-var handoff of paths and settings
 *   - start / stop / liveness check
 *
 * All methods are safe to call from the main thread. Python does its work
 * on a daemon thread, so start() returns quickly.
 */
public final class ServerController {

    private static final String TAG = "defxult";
    private static final String ASSETS_VERSION = "1.0";

    private static int boundPort = -1;
    private static String boundToken = null;
    private static String boundUrl = null;

    private ServerController() {}

    public static final class StartResult {
        public final boolean ok;
        public final int port;
        public final String url;
        public final String error;

        StartResult(boolean ok, int port, String url, String error) {
            this.ok = ok;
            this.port = port;
            this.url = url;
            this.error = error;
        }
    }

    /** Ensure CPython is up and assets are extracted. Idempotent. */
    private static void ensurePython(Context ctx) throws IOException {
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(ctx));
        }

        File filesDir = ctx.getFilesDir();
        File nodeModules = new File(filesDir, "node_modules");
        File marker = new File(filesDir, ".assets_version");

        if (!marker.exists() || !ASSETS_VERSION.equals(readFile(marker))) {
            Log.i(TAG, "extracting assets (first launch or upgrade)");
            if (nodeModules.exists()) deleteRecursive(nodeModules);
            extractAssets(ctx, "node_modules", nodeModules);
            writeFile(marker, ASSETS_VERSION);
        }
    }

    /**
     * Start the server. Caller must have already confirmed that a usable
     * network is present via NetworkCheck.
     */
    public static StartResult start(Context ctx) {
        try {
            ensurePython(ctx);

            File filesDir = ctx.getFilesDir();
            File nodeModules = new File(filesDir, "node_modules");
            File rootDir = Environment.getExternalStorageDirectory();

            int wantedPort = Prefs.getPort(ctx);
            String uploadDest = Prefs.getUploadDest(ctx);

            Python py = Python.getInstance();
            PyObject osMod = py.getModule("os");
            PyObject environ = osMod.get("environ");
            environ.callAttr("__setitem__", "DEFXULT_ROOT", rootDir.getAbsolutePath());
            environ.callAttr("__setitem__", "DEFXULT_HOME", filesDir.getAbsolutePath());
            environ.callAttr("__setitem__", "DEFXULT_NODE_MODULES", nodeModules.getAbsolutePath());
            environ.callAttr("__setitem__", "DEFXULT_UPLOAD_DEST", uploadDest);

            // Force a re-import so module-level os.environ reads pick up the
            // new values. Chaquopy caches imported modules.
            try {
                PyObject sys = py.getModule("sys");
                PyObject modules = sys.get("modules");
                modules.callAttr("__delitem__", "defxult_transfer");
            } catch (Exception ignored) {
                // Not previously imported — nothing to drop.
            }

            PyObject mod = py.getModule("defxult_transfer");
            PyObject result = mod.callAttr("start_server", wantedPort);
            List<PyObject> items = result.asList();
            boundPort = items.get(0).toJava(int.class);
            boundToken = items.get(1).toJava(String.class);

            String ip = mod.callAttr("get_local_ip").toJava(String.class);
            boundUrl = "http://" + ip + ":" + boundPort + "/?t=" + boundToken;

            if (!isAlive(boundPort)) {
                return new StartResult(false, boundPort, boundUrl, "Server did not bind");
            }

            Prefs.setRunning(ctx, true);
            Log.i(TAG, "server up: " + boundUrl);
            return new StartResult(true, boundPort, boundUrl, null);

        } catch (Exception e) {
            Log.e(TAG, "start failed", e);
            String msg = (e.getMessage() == null) ? e.toString() : e.getMessage();
            return new StartResult(false, -1, null, msg);
        }
    }

    /** Stop the server. Idempotent. */
    public static void stop(Context ctx) {
        try {
            Python py = Python.getInstance();
            PyObject mod = py.getModule("defxult_transfer");
            mod.callAttr("stop_server");
        } catch (Exception e) {
            Log.w(TAG, "stop failed (probably already down)", e);
        }
        boundPort = -1;
        boundToken = null;
        boundUrl = null;
        Prefs.setRunning(ctx, false);
    }

    /** Cheap liveness check: try to open a socket to the loopback port. */
    public static boolean isAlive(int port) {
        if (port <= 0) return false;
        try (Socket s = new Socket()) {
            s.connect(new InetSocketAddress("127.0.0.1", port), 200);
            return true;
        } catch (IOException e) {
            return false;
        }
    }

    public static int getBoundPort() { return boundPort; }
    public static String getBoundUrl() { return boundUrl; }

    /**
     * Called on app launch. If Prefs says a server should be running AND it
     * actually is, returns true. Otherwise resets the flag and returns false.
     */
    public static boolean checkResume(Context ctx) {
        if (!Prefs.isRunning(ctx)) return false;
        int port = Prefs.getPort(ctx);
        if (isAlive(port)) {
            boundPort = port;
            return true;
        }
        Prefs.setRunning(ctx, false);
        return false;
    }

    // ---------- asset helpers ----------

    private static void extractAssets(Context ctx, String assetPath, File target) throws IOException {
        String[] children = ctx.getAssets().list(assetPath);
        if (children == null || children.length == 0) {
            File parent = target.getParentFile();
            if (parent != null) parent.mkdirs();
            try (InputStream in = ctx.getAssets().open(assetPath);
                 FileOutputStream out = new FileOutputStream(target)) {
                byte[] buf = new byte[65536];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            }
        } else {
            target.mkdirs();
            for (String child : children) {
                extractAssets(ctx, assetPath + "/" + child, new File(target, child));
            }
        }
    }

    private static void deleteRecursive(File f) {
        if (f.isDirectory()) {
            File[] kids = f.listFiles();
            if (kids != null) for (File k : kids) deleteRecursive(k);
        }
        f.delete();
    }

    private static String readFile(File f) throws IOException {
        try (FileInputStream in = new FileInputStream(f)) {
            byte[] b = new byte[(int) f.length()];
            int off = 0;
            while (off < b.length) {
                int n = in.read(b, off, b.length - off);
                if (n < 0) break;
                off += n;
            }
            return new String(b, 0, off, "UTF-8");
        }
    }

    private static void writeFile(File f, String s) throws IOException {
        try (FileOutputStream out = new FileOutputStream(f)) {
            out.write(s.getBytes("UTF-8"));
        }
    }
}