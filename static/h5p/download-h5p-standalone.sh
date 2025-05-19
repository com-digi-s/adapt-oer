#!/bin/bash

# Get the latest release data
release_data=$(curl -s https://api.github.com/repos/tunapanda/h5p-standalone/releases/latest)

# Extract the version (without the "v" prefix)
version=$(echo "$release_data" | grep "tag_name" | cut -d : -f 2 | tr -d \"\,\v )

# Extract the download URL
download_url=$(echo "$release_data" | grep "browser_download_url" | cut -d : -f 2,3 | tr -d \" )

# Form the output filename
output_filename="./h5p-standalone.zip"

# Download the zip file
curl -L -o $output_filename $download_url

# Extract the directory name from the zip file (remove .zip extension)
dir_name=$(echo "$output_filename" | sed 's/\.zip$//')

# Create a directory with the name
mkdir -p "$dir_name"

# Unzip the downloaded file into the directory
unzip -o $output_filename -d "$dir_name"

# Remove the temporary zip file
rm $output_filename