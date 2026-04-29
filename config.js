// =============================================================================
// Bot configuration — edit values here instead of using env variables.
// Environment variables (if set) take precedence over these defaults.
// =============================================================================

module.exports = {
    // WhatsApp number used to PAIR the bot (digits only, with country code,
    // no "+" or "00"). Example for Morocco: "212688898322"
    PHONE_NUMBER: "212688898322",

    // Developer phone number — only this user can run /cookie commands.
    DEVELOPER_NUMBER: "212688898322",

    // WhatsApp's new "Linked Identity" (LID) format hides real phone numbers.
    // Add your LID(s) here, comma-separated. Find yours in the bot logs:
    // "Message from <LID>@lid: ..."
    // Example: "187136791855332" or "187136791855332,123456789012345"
    DEVELOPER_LID: "187136791855332",

    // Internal Flask server URL — usually no need to change this.
    GEMINI_SERVER: "http://127.0.0.1:5000",

    // Optional shared secret for /admin/* endpoints.
    // Leave empty unless you set ADMIN_TOKEN on the Flask server too.
    ADMIN_TOKEN: "",
};
