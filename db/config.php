<?php
/**
 * Database configuration.
 *
 * DO NOT hardcode credentials here.
 * Read them from environment variables set by your hosting provider.
 *
 * This file is kept for reference only and is NOT used by the Django application.
 * The Django application reads all database settings from environment variables
 * via config/settings.py.
 */
define('DB_HOST', getenv('DB_HOST') ?: '127.0.0.1');
define('DB_NAME', getenv('DB_NAME') ?: '');
define('DB_USER', getenv('DB_USER') ?: '');
define('DB_PASS', getenv('DB_PASSWORD') ?: '');

function db() {
    static $pdo;
    if (!$pdo) {
        if (!DB_PASSWORD || !DB_USER) {
            throw new RuntimeException('Database credentials are not configured.');
        }
        $pdo = new PDO(
            'mysql:host=' . DB_HOST . ';dbname=' . DB_NAME . ';charset=utf8mb4',
            DB_USER,
            DB_PASS,
            [
                PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            ]
        );
    }
    return $pdo;
}