package com.myagent.app

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Relay URL + token in the app's private SharedPreferences. The token is additionally
 *  encrypted with an AES key that lives in the Android Keystore and never leaves the device,
 *  so a copied prefs file (backup, rooted dump) doesn't reveal it. Backups are also disabled
 *  in the manifest. Nothing here is ever logged. */
class SecureStore(context: Context) {
    private val prefs = context.getSharedPreferences("myagent", Context.MODE_PRIVATE)

    var url: String
        get() = prefs.getString(K_URL, "") ?: ""
        set(v) { prefs.edit().putString(K_URL, v).apply() }

    var token: String
        get() {
            prefs.getString(K_LEGACY_TOKEN, null)?.let { plain ->   // stored in plain text by v1.0
                token = plain
                return plain
            }
            val enc = prefs.getString(K_TOKEN, null) ?: return ""
            return try { decrypt(enc) } catch (e: Exception) {
                prefs.edit().remove(K_TOKEN).apply()   // key gone (e.g. restored on a new phone)
                ""
            }
        }
        set(v) {
            val e = prefs.edit().remove(K_LEGACY_TOKEN)
            if (v.isEmpty()) e.remove(K_TOKEN) else e.putString(K_TOKEN, encrypt(v))
            e.apply()
        }

    fun clear() {
        prefs.edit().clear().apply()
        try { keystore().deleteEntry(ALIAS) } catch (_: Exception) {}
    }

    private fun keystore() = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun key(): SecretKey {
        (keystore().getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build())
        return gen.generateKey()
    }

    private fun encrypt(plain: String): String {
        val c = Cipher.getInstance(AES_GCM)
        c.init(Cipher.ENCRYPT_MODE, key())
        val out = c.iv + c.doFinal(plain.toByteArray(Charsets.UTF_8))
        return Base64.encodeToString(out, Base64.NO_WRAP)
    }

    private fun decrypt(enc: String): String {
        val raw = Base64.decode(enc, Base64.NO_WRAP)
        val c = Cipher.getInstance(AES_GCM)
        c.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, raw, 0, IV_LEN))
        return String(c.doFinal(raw, IV_LEN, raw.size - IV_LEN), Charsets.UTF_8)
    }

    private companion object {
        const val ALIAS = "myagent-token-key"
        const val AES_GCM = "AES/GCM/NoPadding"
        const val IV_LEN = 12
        const val K_URL = "url"
        const val K_TOKEN = "token_enc"
        const val K_LEGACY_TOKEN = "token"
    }
}
