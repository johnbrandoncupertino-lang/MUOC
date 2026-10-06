// Helper function for AJAX requests
async function fetchData(url, options = {}) {
    const defaultOptions = {
        headers: {
            "Content-Type": "application/json",
        },
    }

    const mergedOptions = {...defaultOptions, ...options}
    if (options.headers) {
        mergedOptions.headers = {...defaultOptions.headers, ...options.headers}
    }

    try {
        const response = await fetch(url, mergedOptions)
        const data = await response.json()

        if (!response.ok) {
            throw new Error(data.error || "An error occurred")
        }

        return data
    } catch (error) {
        console.error("Fetch error:", error)
        throw error
    }
}

// MUOC confirmation dialog — intentionally styled like the rest of the classic
// banking interface rather than using the default rounded SweetAlert appearance.
function confirmAction(title, text, icon, confirmButtonText, callback) {
    Swal.fire({
        title: title,
        text: text,
        icon: icon,
        showCancelButton: true,
        confirmButtonText: confirmButtonText,
        cancelButtonText: "Cancel",
        reverseButtons: true,
        customClass: {
            popup: "muoc-alert",
            title: "muoc-alert-title",
            htmlContainer: "muoc-alert-text",
            confirmButton: "muoc-alert-confirm",
            cancelButton: "muoc-alert-cancel"
        }
    }).then((result) => {
        if (result.isConfirmed) {
            callback()
        }
    })
}
