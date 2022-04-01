import logging
import os
import re
import csv
import traceback
import urllib.parse
from helpers import *
from arguments import args
from datetime import datetime, timedelta
from time import sleep, time

import requests
from bs4 import BeautifulSoup


def main() -> None:
    # get command line parmeter info

    # set up logger
    logging.root.setLevel(level=args.log)
    logger = logging.getLogger(
        name="pid: " + str(os.getpid()) + "  filename:" + os.path.basename(__file__)
    )
    logging.basicConfig()

    # make cache directories if not present
    case_html_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "data", args.county, "case_html"
    )
    os.makedirs(case_html_path, exist_ok=True)

    main_page = ""
    odyssey_version = ""
    # parse county portal from csv file
    with open(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "resources", "texas_county_data.csv"
        ),
        mode="r",
    ) as file_handle:
        csv_file = csv.DictReader(file_handle)
        for row in csv_file:
            if row["county"].lower() == args.county.lower():
                main_page = row["portal"]
                odyssey_version = int(row["version"].split(".")[0])
                break
        if main_page == "":
            raise ValueError("There is no portal page for this county.")

    # disable SSL warnings
    requests.packages.urllib3.disable_warnings(
        requests.packages.urllib3.exceptions.InsecureRequestWarning
    )

    # start session
    session = requests.Session()
    # allow bad ssl
    session.verify = False

    # start scraping
    if odyssey_version < 2017:  # versions 2017 and higher have different logic
        main_text, failed = request_page_with_retry(
            session=session,
            url=main_page,
            verification_text="ssSearchHyperlink",
            logger=logger,
            ms_wait=args.ms_wait,
        )
        if failed:
            write_debug_and_quit(
                "ssSearchHyperlink", main_text, f"{main_page = }", logger
            )
        main_soup = BeautifulSoup(main_text, "html.parser")
        # get path to the calendar page here
        search_page_links = main_soup.select("a.ssSearchHyperlink")
        for link in search_page_links:
            if link.text == "Court Calendar":
                search_page_id = link["href"].split("?ID=")[1].split("'")[0]
        if not search_page_id:
            write_debug_and_quit("Court Calendar page ID", main_text, "", logger)
        calendar_url = main_page + "Search.aspx?ID=" + search_page_id
        calendar_text, failed = request_page_with_retry(
            session=session,
            url=calendar_url,
            verification_text="Court Calendar",
            logger=logger,
            ms_wait=args.ms_wait,
        )
        if failed:
            write_debug_and_quit(
                "Court Calendar", calendar_text, f"{calendar_url = }", logger
            )
        calendar_soup = BeautifulSoup(calendar_text, "html.parser")
    else:  # versions 2017 and higher have different logic
        main_text, failed = request_page_with_retry(
            session=session,
            url=main_page,
            verification_text="portlet-buttons",
            logger=logger,
            ms_wait=args.ms_wait,
        )
        if failed:
            write_debug_and_quit(
                "portlet-buttons", main_text, f"{main_page = }", logger
            )
        main_soup = BeautifulSoup(main_text, "html.parser")
        # get path to the search page here
        try:
            search_page_relative_url = main_soup.select("a.portlet-buttons")[1]["href"]
        except Exception:
            print(traceback.format_exc())
            write_debug_and_quit("Search page url", main_text, "", logger)
        search_url = urllib.parse.urljoin(main_page, search_page_relative_url)
        search_text, failed = request_page_with_retry(
            session=session,
            url=search_url,
            verification_text="btnHSSubmit",
            logger=logger,
            ms_wait=args.ms_wait,
        )
        if failed:
            write_debug_and_quit(
                "btnHSSubmit", search_text, f"{calendar_url = }", logger
            )
        calendar_soup = BeautifulSoup(search_text, "html.parser")

    # we need these hidden values to access the search page
    hidden_values = {
        hidden["name"]: hidden["value"]
        for hidden in calendar_soup.select('input[type="hidden"]')
        # if "name" in hidden
        # TODO: need to figure this out for >2017, the above line breaks <2017
    }
    # get a list of JOs to their IDs from the search page
    judicial_officer_to_ID = {
        option.text: option["value"]
        for option in calendar_soup.select(
            'select[labelname="Judicial Officer:"] > option'
            if odyssey_version < 2017
            else 'select[id="selHSJudicialOfficer"] > option'
        )
        if option.text
    }
    # if juidicial_officers param is not specified, use all of them
    if not args.judicial_officers:
        args.judicial_officers = list(judicial_officer_to_ID.keys())
    # get nodedesc and nodeid information from main page location select box
    location_option = main_soup.findAll("option", text=re.compile(args.location))[0]
    hidden_values.update(
        {"NodeDesc": args.location, "NodeID": location_option["value"]}
    )

    # initialize some variables
    START_TIME = time()
    cached_case_html_list = [
        file_name.split(".")[0] for file_name in os.listdir(case_html_path)
    ]
    day_count = args.end_date - args.start_date
    day_count = day_count.days

    # days in the past starting with yesterday.
    for date_to_process in (args.start_date + timedelta(n) for n in range(day_count)):
        date_string = datetime.strftime(date_to_process, "%m/%d/%Y")
        for JO_name in args.judicial_officers:
            # error check and initialize variables for this JO
            if JO_name not in judicial_officer_to_ID:
                logger.error(
                    f"judicial officer {JO_name} not found on calendar page. Continuing."
                )
                continue
            JO_id = judicial_officer_to_ID[JO_name]
            logger.info(f"Searching cases on {date_string} for {JO_name}")
            cal_text, failed = request_page_with_retry(
                session=session,
                url=calendar_url,
                verification_text="Record Count",
                logger=logger,
                data=make_form_data(date_string, JO_id, hidden_values),
                ms_wait=args.ms_wait,
            )

            if failed:
                write_debug_and_quit(
                    "Record Count", cal_text, f"{JO_name = }\n{date_string = }", logger
                )

            # rate limiting
            sleep(args.ms_wait / 1000)

            # read the case URLs from the calendar page html
            cal_soup = BeautifulSoup(cal_text, "html.parser")
            case_anchors = cal_soup.select('a[href^="CaseDetail"]')
            logger.info(f"{len(case_anchors)} cases found.")
            # if there are any cases found for this JO and date
            if case_anchors:
                # if all cases are cached, continue
                if (
                    all(
                        case_anchor["href"].split("=")[1] in cached_case_html_list
                        for case_anchor in case_anchors
                    )
                    and not args.overwrite
                ):
                    logger.info("All cases are cached for this JO and date.")
                    continue

                # process each case
                for case_anchor in case_anchors:
                    case_url = main_page + case_anchor["href"]
                    case_id = case_url.split("=")[1]
                    # continue if case is already cached, unless using overwrite flag
                    if case_id in cached_case_html_list and not args.overwrite:
                        continue
                    case_html_file_path = os.path.join(
                        case_html_path, f"{case_id}.html"
                    )

                    # make request for the case
                    logger.info("Visiting: " + case_url)
                    case_results, failed = request_page_with_retry(
                        session=session,
                        url=case_url,
                        verification_text="Date Filed",
                        logger=logger,
                        ms_wait=args.ms_wait,
                    )
                    # error check based on text in html result.
                    if not failed:
                        logger.info(f"Response string length: {len(case_results)}")
                        with open(case_html_file_path, "w") as file_handle:
                            file_handle.write(case_results)
                        # add case id to cached list
                        if case_id not in cached_case_html_list:
                            cached_case_html_list.append(case_id)
                    else:
                        curr_vars = f"{date_string = }\n{JO_name = }\n{case_url = }"
                        write_debug_and_quit(
                            "Date Filed", case_results, curr_vars, logger
                        )
                    # rate limiting - convert ms to seconds
                    sleep(args.ms_wait / 1000)

    logger.info(f"\nTime to run script: {round(time() - START_TIME, 2)} seconds")


if __name__ == "__main__":
    main()