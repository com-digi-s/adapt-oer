    // Function to update heights
    function updateHeights() {
        // Select the elements
        let elements = document.querySelectorAll('.matching > div, .matching > ul > div');

        let maxHeight = 0;

        // Find the maximum height
        elements.forEach((element) => {
            // Ensure the height is auto before getting the scrollHeight
            element.style.height = 'auto';
            let currentHeight = element.scrollHeight;
            maxHeight = Math.max(maxHeight, currentHeight);
        });

        // Convert the maximum height from pixels to rem (assuming 16px as the default root font-size)
        let maxHeightInRem = maxHeight / 16 + 'rem';

        // Apply the maximum height to all elements
        elements.forEach((element) => {
            element.style.height = maxHeightInRem;
        });
    }

    // Initial update of heights
    updateHeights();

    // Update heights when the window is resized
    window.addEventListener('resize', updateHeights);