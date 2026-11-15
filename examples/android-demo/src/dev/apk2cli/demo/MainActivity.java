package dev.apk2cli.demo;

import android.app.Activity;
import android.os.Bundle;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import org.json.JSONObject;

/** Small, owned test app: sign in normally, then persist an app-private session. */
public final class MainActivity extends Activity {
    private TextView status;
    private Button signIn;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        layout.setPadding(40, 72, 40, 40);
        status = new TextView(this);
        status.setTextSize(22);
        status.setText("Signed out");
        status.setContentDescription("apk2cli-status");
        signIn = new Button(this);
        signIn.setText("Sign in to demo");
        signIn.setContentDescription("apk2cli-sign-in");
        signIn.setOnClickListener(view -> login());
        layout.addView(status);
        layout.addView(signIn);
        setContentView(layout);
        if (getPreferencesToken().length() > 0) {
            status.setText("Signed in as demo");
        }
    }

    private String getPreferencesToken() {
        return getSharedPreferences("session", MODE_PRIVATE).getString("access_token", "");
    }

    private void login() {
        signIn.setEnabled(false);
        status.setText("Signing in");
        String baseUrl = getIntent().getStringExtra("base_url");
        if (baseUrl == null || !baseUrl.matches("http://127\\.0\\.0\\.1:[0-9]+")) {
            status.setText("Missing local demo server");
            signIn.setEnabled(true);
            return;
        }
        new Thread(() -> {
            HttpURLConnection connection = null;
            try {
                connection = (HttpURLConnection) new URL(baseUrl + "/login").openConnection();
                connection.setRequestMethod("POST");
                connection.setConnectTimeout(10000);
                connection.setReadTimeout(10000);
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json");
                byte[] credentials = "{\"username\":\"demo\",\"password\":\"fixture-only\"}"
                    .getBytes(StandardCharsets.UTF_8);
                try (OutputStream output = connection.getOutputStream()) {
                    output.write(credentials);
                }
                if (connection.getResponseCode() != 200) {
                    throw new IllegalStateException("Login rejected");
                }
                ByteArrayOutputStream response = new ByteArrayOutputStream();
                try (InputStream input = connection.getInputStream()) {
                    byte[] buffer = new byte[1024];
                    int count;
                    while ((count = input.read(buffer)) != -1) {
                        response.write(buffer, 0, count);
                    }
                }
                JSONObject session = new JSONObject(response.toString("UTF-8"));
                String token = session.getString("access_token");
                boolean saved = getSharedPreferences("session", MODE_PRIVATE).edit()
                    .putString("access_token", token).commit();
                if (!saved) {
                    throw new IllegalStateException("Session storage failed");
                }
                runOnUiThread(() -> {
                    status.setText("Signed in as demo");
                    signIn.setEnabled(true);
                });
            } catch (Exception ignored) {
                // Neither response bodies nor credentials belong in device logs.
                runOnUiThread(() -> {
                    status.setText("Sign-in failed");
                    signIn.setEnabled(true);
                });
            } finally {
                if (connection != null) {
                    connection.disconnect();
                }
            }
        }, "demo-login").start();
    }
}
