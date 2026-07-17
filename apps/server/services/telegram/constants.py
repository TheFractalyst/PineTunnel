# Add License flow
(
    ADD_LIC_NAME,
    ADD_LIC_EMAIL,
    ADD_LIC_FEATURES,
    ADD_LIC_EXPIRY,
    ADD_LIC_CONFIRM,
) = range(5)

# Edit License flow
EDIT_LIC_FIELD, EDIT_LIC_VALUE = range(10, 12)

# Edit License picker flow (button-based)
EDIT_LIC_PICK = 12

# Set Expiry flow
EXPIRY_VALUE = 50

# Expiry picker flow (button-based)
EXPIRY_PICK = 51

# Search flow
SEARCH_QUERY = 70

# Conversation state prefixes to clean up on cancel/timeout
CONVERSATION_CLEANUP_PREFIXES = [
    "new_lic_",
    "edit_lic_",
    "edit_field",
    "expiry_",
    "expiry_key",
]
