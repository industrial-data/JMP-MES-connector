# MES data retrieval add-in - Aspentech IP.21 and Osisoft PI (Aveva)

Aspentech IP.21 and Osisoft PI connector to JMP (SAS Institute)

:inbox_tray: [Download latest version](https://github.com/industrial-data/JMP-MES-connector/raw/main/MES%20Data%20Retrieval%20(IP21-PI)_v_latest.jmpaddin)

This add-in automates data extraction tasks from Aspentech IP.21 and Osisoft PI (Aveva) historians.

This way, you can use JMP to diagnose manufacturing problems and monitor several tags daily and weekly. See our [review](https://pubs.rsc.org/en/content/articlelanding/2022/re/d1re00541c) for more industrial data science applications.

If you would like JMP to support historians natively, give it a thumbs up in the [wishlist](https://community.jmp.com/t5/JMP-Wish-List/Native-Support-to-Manufacturing-Historians-Aspentech-IP-21-and/idi-p/540846).

# Requirements

- JMP v15 or higher
- Aspen Manufacturing Suite (Aspen InfoPlus.21®) and its ODBC driver (SQL Plus).
- OSIsoft PI Client Tools (PI Process Book) and its [PI OLEDB driver](https://techsupport.osisoft.com/Products/PI-System-Access/PI-OLEDB-Enterprise/Overview).
- Access to the server (via Window's active directory)
- Be connected to your enterprise network (usually VPN).

# Functionalities

![](media/image1.png)

The **interface** allows users to:

1. Select a server from a server list or manual input (edit server address)
2. Find tags showing them in a table using either tag name or description
3. Add selected tags or paste tag names when copied from a spreadsheet
4. Filter by start and end date (now and 24 h earlier by default)
5. Select the extraction type (interpolated, average and actual values)
6. Select the extraction period (10 minutes by default)

The extraction is done tag by tag with a high limit of rows (100,000) per tag to not saturate the server.

You can check the log to track the progress of the data extraction. It should be fast enough for most cases unless limited by connectivity speed (e.g., VPN). Please notify in advance your OT/IT team if you are planning for massive data extractions.

After you extract data for both IP21/PI servers, you can UPDATE, REFRESH, and ADD new tags to the table with one click.

![](media/image4.png)

The timestamp in UTC format (TS\_UTC column) will be used, so keep it in your table. Likewise, if you want to change the names of your tag columns, we recommend creating new columns using formulas to refer to them.

# What's the difference between update() and refresh()?

Both will automatically obtain recent data from MES.

- UPDATE increments the table size (adding new rows below)
- REFRESH keeps the same table size (updating first and then deleting old data from the top, so it maintains the number of rows).

Use update() to keep track of all new and old data.

# How can I organize all the tags (columns) after the extraction?

You can easily group and ungroup the tags with our Column Organizer add-in

[Download](https://github.com/industrial-data/JMP-MES-connector/raw/main/column_organizer/Column%20organizer%20v220905.jmpaddin)

![](media/image2.png)

# License

This add-in is open-source (BSD clause 3).

# Serverlist

If you have a list of servers, you can load it using the example provided on GitHub. Notice the "MES\_serverlist.csv" should have ";" instead of "," to work.

To have as a new default your own server list, change the address in the GUI editor within the source code.

# Creating your add-in

You can modify the source code and GUI at your will. The requisite is to keep the specific ID as "MES\_Addin". The steps to go from source code to add-in can be seen below.

![](media/image3.png)

# Roadmap

We have several improvements in mind.

- Code refactoring (IP21/PI data steps as external functions to avoid code duplication).
- Migrate to RestAPI authentication
- Add filters by tag value (e.g., product grade)
- Add more extraction options

Meanwhile, suggestions, issues, and pull requests are more than welcome.

If you are only using PI, you should check a similar add-in with some of these features already developed.

Aveva/OSISoft PI Tools from @IH

[https://community.jmp.com/t5/JMP-Add-Ins/Aveva-OSISoft-PI-Tools/ta-p/224525](https://community.jmp.com/t5/JMP-Add-Ins/Aveva-OSISoft-PI-Tools/ta-p/224525)

