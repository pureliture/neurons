package com.local.ragingressqueue.ingest.domain.validation;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HexFormat;
import java.util.regex.Pattern;

public final class ContentHashVerifier {
    private static final Pattern SHA_256 = Pattern.compile("^sha256:[a-f0-9]{64}$");

    private ContentHashVerifier() {
    }

    public static boolean hasCanonicalShape(String contentHash) {
        return contentHash != null && SHA_256.matcher(contentHash).matches();
    }

    public static boolean matches(String body, String contentHash) {
        return body != null && hasCanonicalShape(contentHash) && sha256Hex(body).equals(contentHash);
    }

    public static String sha256Hex(String value) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] bytes = digest.digest(value.getBytes(StandardCharsets.UTF_8));
            return "sha256:" + HexFormat.of().formatHex(bytes);
        } catch (NoSuchAlgorithmException error) {
            throw new IllegalStateException("SHA-256 is not available", error);
        }
    }
}
