# Installation Guide for Windows

## Prerequisites

1. **Python 3.7 or higher**
   - Download from: https://www.python.org/downloads/
   - During installation, check "Add Python to PATH"

2. **GTK+ for Windows** (required for pycairo)
   - Download from: https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer
   - Or use: `pip install pipwin` then `pipwin install pycairo`

## Installation Steps

### Step 1: Install Python
1. Download Python from python.org
2. Run the installer
3. **Important**: Check "Add Python to PATH"
4. Click "Install Now"

### Step 2: Verify Python Installation
Open Command Prompt and run:
```cmd
python --version
```
You should see something like "Python 3.x.x"

### Step 3: Install Dependencies

Open Command Prompt in the folder containing the converter files and run:

```cmd
pip install -r requirements.txt
```

### Alternative: Install packages individually
If the above fails, try installing one by one:

```cmd
pip install pygerber
pip install numpy
pip install pillow
pip install scipy
```

For pycairo on Windows, you may need:
```cmd
pip install pipwin
pipwin install pycairo
```

### Step 4: Test Installation

```cmd
python gerber_to_gcode.py --help
```

You should see the help message with all available options.

## Quick Start

### Using Python directly:
```cmd
python gerber_to_gcode.py input.gtl -o output.nc --invert
```

### Using the batch file wrapper:
```cmd
gerber2gcode.bat input.gtl -o output.nc --invert
```

## Common Windows Issues

### Issue: "python is not recognized"
**Solution**: Python is not in your PATH. Reinstall Python and check "Add Python to PATH"

### Issue: "pip is not recognized"
**Solution**: 
```cmd
python -m pip install -r requirements.txt
```

### Issue: pycairo installation fails
**Solution**: Try using pipwin:
```cmd
pip install pipwin
pipwin install pycairo
```

Or download precompiled wheels from:
https://www.lfd.uci.edu/~gohlke/pythonlibs/#pycairo

### Issue: "No module named 'gerber'"
**Solution**: Install pygerber:
```cmd
pip install pygerber
```

### Issue: Permission denied errors
**Solution**: Run Command Prompt as Administrator

## Testing Your Installation

1. Create a test Gerber file or use one from your PCB design
2. Run the converter:
```cmd
python gerber_to_gcode.py test.gtl -o test.nc --invert
```
3. Check that test.nc was created
4. Open test.nc in a text editor or G-code viewer

## Recommended G-code Viewers for Windows

- **Candle** - Great GRBL sender with visualization
- **Universal G-code Sender** - Cross-platform, feature-rich
- **bCNC** - Python-based, very capable
- **OpenBuilds CONTROL** - Simple and effective

## Setting Up for Easy Use

### Option 1: Add to PATH
1. Copy all files to a permanent location (e.g., `C:\PCBTools\`)
2. Add that folder to your Windows PATH
3. Now you can run `gerber2gcode.bat` from anywhere

### Option 2: Create Desktop Shortcut
1. Right-click on `gerber2gcode.bat`
2. Create shortcut
3. Move shortcut to desktop
4. Drag and drop Gerber files onto the shortcut

## Getting Help

View all options:
```cmd
python gerber_to_gcode.py --help
```

View examples:
```cmd
python example_usage.py
```

## Next Steps

1. Read README.md for detailed usage instructions
2. Review example_usage.py for common scenarios
3. Start with conservative settings and test on scrap
4. Adjust parameters based on your specific tool and material

## Uninstallation

To remove the tool:
1. Delete the folder containing the scripts
2. (Optional) Uninstall Python packages:
```cmd
pip uninstall pygerber numpy pillow scipy pycairo
```
