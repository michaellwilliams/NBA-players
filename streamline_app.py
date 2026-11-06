import streamlit as st
import pandas as pd
from neo4j import GraphDatabase
from streamlit_agraph import agraph, Node, Edge, Config
import pycountry
import openai
import os
import time



# ==========================================
# ADMINISTRATOR PASSWORD PROTECTION
# ==========================================

def require_admin_access():

    # Already authorized during this session
    if st.session_state.get("admin_authorized", False):
        return True

    st.warning("Administrator authorization is required.")

    password = st.text_input(
        "Administrator Password",
        type="password",
        key="admin_password_input"
    )

    if st.button("🔐 Unlock Admin Actions", key="unlock_admin_actions"):

        if password == st.secrets["ADMIN_PASSWORD"]:
            st.session_state["admin_authorized"] = True
            st.success("Administrator access granted.")
            return True

        else:
            st.error("Incorrect administrator password.")

    return False
########################################################

# --- Main UI Setup ---
st.set_page_config(
    page_title="Player Editor Dashboard",
    page_icon="🏀",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Configuration & State List ---
NEO4J_URI = st.secrets["NEO4J_URI"]
NEO4J_USER = st.secrets["NEO4J_USER"]
NEO4J_PASSWORD = st.secrets["NEO4J_PASSWORD"]


# Define your exact desired display order for fields (all lowercase)
PLAYER_FIELD_ORDER = ["name", "team", "college", "country", "season", "age", "draft yr.", "draft no.", "draft rd.", "games", "fg%", "reb", "orb", "drb%", "orb%", "ast", "ast%", "usg%", "net"]


# Helper map to translate incoming Neo4j graph abbreviations to full names
state_to_abbrev_map = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"
}

# Inverse map to write abbreviations back to Neo4j if desired (optional, currently saves full names)
us_states = {v: k for k, v in state_to_abbrev_map.items()}

# GENERATE THE GLOBAL COUNTRY ARRAY BELOW YOUR STATE LIST
world_countries = sorted([country.name for country in pycountry.countries])


nba_teams_map = {
    "ATL": "Atlanta Hawks", "BOS": "Boston Celtics", "BKN": "Brooklyn Nets", "CHA": "Charlotte Hornets", 
    "CHI": "Chicago Bulls", "CLE": "Cleveland Cavaliers", "DAL": "Dallas Mavericks", "DEN": "Denver Nuggets", 
    "DET": "Detroit Pistons", "GSW": "Golden State Warriors", "HOU": "Houston Rockets", "IND": "Indiana Pacers", 
    "LAC": "LA Clippers", "LAL": "Los Angeles Lakers", "MEM": "Memphis Grizzlies", "MIA": "Miami Heat", 
    "MIL": "Milwaukee Bucks", "MIN": "Minnesota Timberwolves", "NOP": "New Orleans Pelicans", "NYK": "New York Knicks", 
    "OKC": "Oklahoma City Thunder", "ORL": "Orlando Magic", "PHI": "Philadelphia 76ers", "PHX": "Phoenix Suns", 
    "POR": "Portland Trail Blazers", "SAC": "Sacramento Kings", "SAS": "San Antonio Spurs", "TOR": "Toronto Raptors", 
    "UTA": "Utah Jazz", "WAS": "Washington Wizards"
}

# FIXED POSITION: Moved this above the loaders so they can safely call it
def normalize_dataframe_data(df_raw):
    if "state" in df_raw.columns:
        df_raw["state"] = df_raw["state"].map(lambda x: state_to_abbrev_map.get(str(x).upper(), x) if pd.notna(x) else x)
    if "country" in df_raw.columns:
        df_raw["country"] = df_raw["country"].map(clean_country_name)
    if "team" in df_raw.columns:
        df_raw["team"] = df_raw["team"].map(match_nba_dropdown_value)
    return df_raw


# Generate a list for dropdown menus: "LAL - Los Angeles Lakers"
nba_dropdown_options = sorted([f"{abbr} - {name}" for abbr, name in nba_teams_map.items()])

# Helper function to convert country abbreviations (US, USA, etc.) to full names
def clean_country_name(val):
    if pd.isna(val) or val == "":
        return ""
    val_str = str(val).strip()
    
    # Try looking up as 2-letter or 3-letter ISO code
    if len(val_str) == 2:
        match = pycountry.countries.get(alpha_2=val_str.upper())
        if match: return match.name
    elif len(val_str) == 3:
        match = pycountry.countries.get(alpha_3=val_str.upper())
        if match: return match.name
        
    # Fallback search by full name to catch alternate spellings safely
    try:
        match = pycountry.countries.lookup(val_str)
        return match.name
    except (LookupError, AttributeError):
        return val_str # Return original if lookup completely fails

# Helper function to cleanly match a messy graph team string to our "ABBR - Name" dropdown format
def match_nba_dropdown_value(val):
    if pd.isna(val) or val == "":
        return ""
    val_str = str(val).strip().upper()
    
    # Try mapping by raw abbreviation look-up (e.g. "LAL")
    if val_str in nba_teams_map:
        return f"{val_str} - {nba_teams_map[val_str]}"
        
    # Try mapping by full name lookup matching (e.g. "Los Angeles Lakers")
    for abbr, name in nba_teams_map.items():
        if val_str == name.upper() or val_str in name.upper():
            return f"{abbr} - {name}"
            
    # Try checking if it's already structured correctly
    if " - " in val_str:
        return val

    return val


# --- Database Drivers & Functions ---

@st.cache_resource
def get_driver():
    driver = GraphDatabase.driver(
        NEO4J_URI,
        auth=(NEO4J_USER, NEO4J_PASSWORD),
        connection_timeout=30
    )

    try:
        driver.verify_connectivity()
        return driver

    except Exception:
        driver.close()
        raise


try:
    driver = get_driver()
    st.success("Connected to Neo4j AuraDB!")

except Exception as e:
    st.error("Unable to connect to Neo4j AuraDB.")

    st.write("Exception type:", type(e).__name__)
    st.write("Neo4j error code:", getattr(e, "code", "Not available"))
    st.write("Error message:", str(e))

    st.stop()

def get_all_players(tx):
    query = """
    MATCH (u:Player)
    RETURN elementId(u) AS id, properties(u) AS props
    """
    result = tx.run(query)
    players = []
    for record in result:
        props = record["props"] or {}
        players.append({"id": record["id"], **props})
    return players

def create_user_node(tx, props):
    # FIX: Explicitly ensure the database query sets uppercase keys cleanly
    query = """
    CREATE (p:Player)
    SET p = $props
    RETURN elementId(p) AS id, properties(p) AS props
    """
    result = tx.run(query, props=props)
    return result.single()

def update_user_node(tx, user_id, props):
    query = """
    MATCH (u:Player)
    WHERE elementId(u) = $user_id
    SET u += $props
    """
    tx.run(query, user_id=user_id, props=props)

def delete_user_node(tx, user_id):
    query = """
    MATCH (u:Player)
    WHERE elementId(u) = $user_id
    DETACH DELETE u
    """
    tx.run(query, user_id=user_id)

def load_players():
    with driver.session() as session:
        records = session.execute_read(get_all_players)
    if not records:
        return pd.DataFrame()
        
    # --- FIX: Normalize dictionary keys to lowercase BEFORE creating the DataFrame ---
    clean_records = []
    for row in records:
        clean_row = {}
        for k, v in row.items():
            # Keep 'id' clean, force all other player attribute keys to absolute lowercase
            if str(k).lower() == "id":
                clean_row["id"] = v
            else:
                clean_row[str(k).lower().strip()] = v
        clean_records.append(clean_row)
    # ---------------------------------------------------------------------------------
        
    df_raw = pd.DataFrame(clean_records)
    return normalize_dataframe_data(df_raw)

    
    # Clean up column key casing variations so everything maps to exact matches
    df_raw.columns = [c.lower() if c not in ["id", "Select"] else c for c in df_raw.columns]
    
    # Translate State abbreviations to full names
    if "state" in df_raw.columns:
        df_raw["state"] = df_raw["state"].map(lambda x: state_abbrev_map.get(str(x).upper(), x) if pd.notna(x) else x)
        
    # FIX: Convert incoming abbreviated country codes (US, USA) to full names on load
    if "country" in df_raw.columns:
        df_raw["country"] = df_raw["country"].map(clean_country_name)

    if "team" in df_raw.columns:
        # Standardize spreadsheet display cells to use our descriptive format
        df_raw["team"] = df_raw["team"].map(match_nba_dropdown_value)
        
    return df_raw


def get_player_network_subgraph(tx, player_id):
    # Surgical query: Finds the player node, matches their team and college links, and pulls their team colleagues
    query = """
    MATCH (p:Player)
    WHERE elementId(p) = $player_id
    OPTIONAL MATCH (p)-[:PLAYED_FOR|BELONGS_TO]->(t:Team)
    OPTIONAL MATCH (p)-[:GRADUATED_FROM|ATTENDED]->(c:College)
    OPTIONAL MATCH (teammate:Player)-[:PLAYED_FOR|BELONGS_TO]->(t)
    WHERE elementId(teammate) <> $player_id
    RETURN 
        p.Name AS player_name,
        t.Name AS team_name,
        c.Name AS college_name,
        collect(DISTINCT teammate.Name)[0..7] AS teammates  // Restrict to top 7 elements to keep wheel readable
    """
    result = tx.run(query, player_id=player_id)
    record = result.single()
    if not record:
        return None
    return {
        "name": record["player_name"] or "Selected Player",
        "team": record["team_name"],
        "college": record["college_name"],
        "teammates": record["teammates"] or []
    }


# --- Dialog Windows (Modals) ---
@st.dialog("⚠️ Confirm Deletion")
def confirm_delete_dialog(player_ids):
    st.write(f"Are you sure you want to permanently delete **{len(player_ids)}** selected player(s)?")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Yes, Delete", type="primary", use_container_width=True):
            with driver.session() as session:
                for p_id in player_ids:
                    session.execute_write(delete_user_node, p_id)
            st.success("Deleted!")
            st.rerun()
    with col2:
        if st.button("Cancel", use_container_width=True):
            st.rerun()

@st.dialog("➕ Add New Player", width="large")
def add_player_dialog(columns):
    if not require_admin_access(): # TODO: Make sure this works
        return
    st.write("Fill out the properties for the new player:")
    new_props = {}
    
    # 1. Gather unique lowercase column fields
    raw_cols = list(set([c.lower() for c in columns if c not in ["id", "Select"]]))
    if "country" not in raw_cols: raw_cols.append("country")
    if "team" not in raw_cols: raw_cols.append("team")

    # 2. FIXED: Sort fields dynamically using your master PLAYER_FIELD_ORDER list
    form_cols = sorted(
        raw_cols,
        key=lambda x: PLAYER_FIELD_ORDER.index(x) if x in PLAYER_FIELD_ORDER else len(PLAYER_FIELD_ORDER) + raw_cols.index(x)
    )

    v_num = st.session_state.get("add_form_version", 0)


    for i in range(0, len(form_cols), 3):
        chunk = form_cols[i:i+3]
        grid_cols = st.columns(3)
        for idx, col in enumerate(chunk):
            with grid_cols[idx]:
                w_key = f"add_{col}_v{v_num}"
                if col == "country":
                    new_props[col] = st.selectbox("Country", options=[""] + world_countries, key=w_key)
                elif col == "team":
                    new_props[col] = st.selectbox("NBA Team", options=[""] + nba_dropdown_options, key=w_key)
                else:
                    new_props[col] = st.text_input(col.title(), key=w_key)
                    
  
    st.markdown("---")
    add_btn_c1, add_btn_c2, add_btn_c3 = st.columns(3)
    
    with add_btn_c1:
        if st.button("💾 Save New Player", type="primary", use_container_width=True):
            clean_props = {k: v for k, v in new_props.items() if v != "" and pd.notna(v)}
                
            if len(clean_props) > 0:
                final_db_props = {}
                for k, v in clean_props.items():
                                     # 1. Standardize text and advanced stat property capitalization for the database
                    db_key = k
                    norm_k = k.lower().strip()
                        
                    if norm_k == "team": db_key = "Team"
                    elif norm_k == "country": db_key = "Country"
                    elif norm_k == "college": db_key = "College"
                    elif norm_k == "name": db_key = "Name"
                    elif norm_k == "season": db_key = "Season"
                    elif norm_k == "games": db_key = "Games"
                    elif norm_k == "reb": db_key = "REB"
                    elif norm_k == "ast": db_key = "AST"
                    elif norm_k == "net": db_key = "NET"
                    elif norm_k == "fg%": db_key = "FG%"
                    elif norm_k == "draft yr.": db_key = "Draft Yr."
                    elif norm_k == "draft no.": db_key = "Draft No."
                    elif norm_k == "draft rd.": db_key = "Draft Rd."
                    # --- FIX: Map the remaining advanced rate percentage fields cleanly ---
                    elif norm_k in ["drb%", "dry%"]: db_key = "DRB%"  # Catches both typo and correct formats
                    elif norm_k == "orb%": db_key = "ORB%"
                    elif norm_k == "ast%": db_key = "AST%"
                    elif norm_k == "usg%": db_key = "USG%"
                    # ----------------------------------------------------------------------

                    # 2. SAFE NUMBER CONVERSION: Handle decimals (floats) and integers cleanly
                    val_str = str(v).strip()
                    if val_str.replace('.', '', 1).isdigit():
                        if '.' in val_str:
                            final_db_props[db_key] = float(val_str)
                        else:
                            final_db_props[db_key] = int(val_str)
                    else:
                        # Keep text strings as strings
                        final_db_props[db_key] = v

                with driver.session() as session:
                    session.execute_write(create_user_node, final_db_props)
                    
                # Reset states and force UI table reload
                st.session_state["clear_checkboxes"] = True
                if "dynamic_key" in st.session_state:
                    st.session_state["dynamic_key"] = f"editor_{int(time.time())}"
                    
                st.success("Player created successfully!")
                st.rerun()
            else:
                st.error("Please fill out at least one property.")

    with add_btn_c2:
        # FIXED CLEAR INFO BUTTON (No more closing the form!)
        if st.button("🧹 Clear Info", use_container_width=True):
            # 1. Clean the old key records out of session state memory
            for col in form_cols:
                old_key = f"add_{col}_v{v_num}"
                if old_key in st.session_state:
                    del st.session_state[old_key]
            
            # 2. Bump the version counter to instantly redraw empty fields on screen
            st.session_state["add_form_version"] += 1
            
            # 3. Use an isolated local fragment refresh instead of st.rerun() to prevent modal closing
            st.rerun(scope="fragment")
        
    with add_btn_c3:
        if st.button("❌ Cancel", use_container_width=True):
            st.rerun()


@st.dialog("📝 Edit Selected Player", width="large")
def edit_player_dialog(player_id, current_props):
    if not require_admin_access(): # TODO: Make sure this works
        return
    # Track the active modal view state explicitly ("EDIT" or "CONFIRM_DELETE")
    view_key = f"modal_view_{player_id}"
    
    # If the user opens this modal for a completely new player, or if it's missing, default to the EDIT form view
    if view_key not in st.session_state or st.session_state.get("last_opened_id") != player_id:
        st.session_state[view_key] = "EDIT"
        st.session_state["last_opened_id"] = player_id

    # --- VIEW A: Inline Deletion Confirmation Page ---
    if st.session_state[view_key] == "CONFIRM_DELETE":
        st.subheader("⚠️ Confirm Permanent Deletion")

        if not require_admin_access(): # TODO: Make sure this works.
            return

        st.write("Are you sure you want to permanently delete this player node?")
        st.warning("This action cannot be undone and will clean all graph relationships.")
        st.markdown("---")
        
        del_c1, del_c2 = st.columns(2)
        with del_c1:
            if st.button("🔥 Yes, Delete From Graph", type="primary", use_container_width=True):
                with driver.session() as session:
                    session.execute_write(delete_user_node, player_id)
                # Cleanup state keys entirely on success
                del st.session_state[view_key]
                st.success("Player purged successfully!")
                st.rerun()
        with del_c2:
            if st.button("Cancel & Go Back", use_container_width=True):
                # Switch view state back to standard EDIT form layout
                st.session_state[view_key] = "EDIT"
                st.rerun()

    elif st.session_state[view_key] == "EDIT":
        st.write("Modify the dynamic values below:")
        updated_props = {}
        
        # 1. Gather all unique lowercase column keys using a set to eliminate case duplicates
        # FIX: Explicitly ignore both lowercase 'select' and uppercase 'Select' when building the form fields
        raw_cols = list(set([col.lower() for col in current_props.keys() if str(col).lower() not in ["id", "select"]]))

        
        if "country" not in raw_cols:
            raw_cols.append("country")
            current_props["country"] = ""

        if "team" not in raw_cols: 
            raw_cols.append("team")
            current_props["team"] = ""

        # 2. Sort fields dynamically based on your custom master PLAYER_FIELD_ORDER list
        form_cols = sorted(
            raw_cols,
            key=lambda x: PLAYER_FIELD_ORDER.index(x) if x in PLAYER_FIELD_ORDER else len(PLAYER_FIELD_ORDER) + raw_cols.index(x)
        )

        # Chunk columns into batches of 3 and distribute them into a 3-column layout grid
        for i in range(0, len(form_cols), 3):
            chunk = form_cols[i:i+3]
            grid_cols = st.columns(3)
            for idx, col in enumerate(chunk):
                orig_key = next((k for k in current_props.keys() if k.lower() == col), col)
                val = current_props.get(orig_key, "")
                
                # FIXED: Dynamic tracking suffix preserves grid rendering and prevents collisions
                w_key = f"edit_{col}_{i+idx}"
                
                with grid_cols[idx]:
                    if col == "country":
                        full_country_name = clean_country_name(val)
                        current_country_idx = world_countries.index(full_country_name) + 1 if full_country_name in world_countries else 0
                        updated_props[orig_key] = st.selectbox("Country", options=[""] + world_countries, index=current_country_idx, key=w_key)
                    elif col == "team":
                        matched_val = match_nba_dropdown_value(val)
                        current_team_idx = nba_dropdown_options.index(matched_val) + 1 if matched_val in nba_dropdown_options else 0
                        updated_props[orig_key] = st.selectbox("NBA Team", options=[""] + nba_dropdown_options, index=current_team_idx, key=w_key)
                    else:
                        # --- CRITICAL FIX: Explicitly treat Season as text string, not a number ---
                        if col == "season":
                            updated_props[orig_key] = st.text_input(col.title(), value=str(val) if pd.notna(val) else "", key=w_key)
                        
                        # Handle missing values (NaN) safely before processing numbers
                        elif pd.isna(val) or val == "" or str(val).lower() == "nan":
                            # Default to an integer 0 if it's a known numeric field, otherwise keep as blank text
                            if col in ["age", "games", "draft yr.", "draft no.", "draft rd."]:
                                updated_props[orig_key] = st.number_input(col.title(), value=0, key=w_key)
                            elif col in ["fg%", "reb", "ast", "drb%", "orb%", "ast%", "usg%", "net"]:
                                updated_props[orig_key] = st.number_input(col.title(), value=0.0, key=w_key)
                            else:
                                updated_props[orig_key] = st.text_input(col.title(), value="", key=w_key)
                        
                        elif isinstance(val, (int, float)) or (isinstance(val, str) and val.replace('.','',1).replace('-','',1).isdigit()):
                            # Handle valid float conversions vs clean integers safely
                            num_val = float(val) if '.' in str(val) else int(float(val))
                            updated_props[orig_key] = st.number_input(col.title(), value=num_val, key=w_key)
                        else:
                            updated_props[orig_key] = st.text_input(col.title(), value=str(val) if pd.notna(val) else "", key=w_key)


                
        st.markdown("---")
        modal_btn_c1, modal_btn_c2, modal_btn_c3 = st.columns(3)
        
        with modal_btn_c1:
            if st.button("💾 Save Changes", type="primary", use_container_width=True):
                # FIX: Build full safe data dictionary ensuring type conversions occur
                final_props = {}
                for k, v in updated_props.items():
                    # --- CRITICAL FIX: Remap lowercase keys back to exact database casing ---
                    db_key = k
                    if k.lower() == "team":
                        db_key = "Team"
                    elif k.lower() == "country":
                        db_key = "Country"
                    elif k.lower() == "college":
                        db_key = "College"
                    elif k.lower() == "name":
                        db_key = "Name"
                    # -------------------------------------------------------------------------

                    if v == "" or pd.isna(v):
                        final_props[db_key] = ""
                    else:
                        # Make sure integer fields stay clean integers for calculations
                        if str(v).isdigit() and db_key.lower() in ["age", "games", "draft yr.", "draft no.", "draft rd."]:
                            final_props[db_key] = int(v)
                        else:
                            final_props[db_key] = v

                # Push to Neo4j using a += MERGE operation to prevent property drops
                with driver.session() as session:
                    session.run(
                        """
                        MATCH (p:Player)
                        WHERE elementId(p) = $player_id
                        SET p += $props
                        """,
                        player_id=player_id,
                        props=final_props
                    )
                # Ensure view defaults back to EDIT next time it opens
                st.session_state[view_key] = "EDIT"
                st.success("Player updated successfully!")
                st.rerun()

                
        with modal_btn_c2:
            if st.button("🗑️ Delete Player", use_container_width=True):
                if require_admin_access(): # TODO: Make sure this works
                    # 1. Flip the state variable strictly to the confirmation string name
                    st.session_state[view_key] = "CONFIRM_DELETE"
                        
                    # 2. Tell the table on the main page to reset its checked boxes
                    st.session_state["clear_checkboxes"] = True
                    st.rerun()
                else:
                    exit

                
        with modal_btn_c3:
            if st.button("❌ Cancel", use_container_width=True):
                st.session_state[view_key] = "EDIT"
                st.rerun()

st.title("🏀 Pro Basketball Player Dashboard Workspace")

# --- Main Dashboard Execution Workspace ---
# FIX: Initialize start_idx here so pagination logic doesn't crash on bootup
if "sort_col" not in st.session_state: st.session_state.sort_col = "name"
if "sort_asc" not in st.session_state: st.session_state.sort_asc = True
if "dynamic_key" not in st.session_state: st.session_state.dynamic_key = "editor_v1"
if "start_idx" not in st.session_state: st.session_state.start_idx = 0


# df = load_players()



# --- Initialize Global Session Variables (PLACE OUTSIDE ELSE BLOCKS) ---
if "add_form_version" not in st.session_state:
    st.session_state["add_form_version"] = 0

# Added for RAG Text Query
# Initialize OpenAI Client (Make sure your OPENAI_API_KEY environment variable is set)
# Alternatively, you can use any local open-source LLM like Ollama here as well
#client = openai.OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "your-api-key-here"))

# FIXED: Instructs Streamlit to securely fetch the token from your secrets.toml file
client = openai.OpenAI(api_key=st.secrets["OPENAI_API_KEY"])


def translate_text_to_cypher(user_prompt):
    system_instruction = """
    You are an expert Neo4j Cypher translator. Your job is to convert natural language 
    questions into a valid, optimized Cypher query matching this exact schema:
    
    Node Label: (:Player)
    
    EXACT DATABASE PROPERTY KEYS (USE THESE EXACTLY, CASE-SENSITIVE):
    - User types 'name' -> Use property: `Name`     
    - User types 'age' -> Use property: `age`
    - User types 'team' -> Use property: `Team`     
    - User types 'country' -> Use property: `Country` 
    - User types 'college' or 'university' -> Use property: `College`
    - User types 'rebounds' -> Use property: `REB`
    - User types 'games' -> Use property: `Games`
    - User types 'field goal percentage' -> Use property: `FG%`
    - User types 'draft year' -> Use property: `Draft Yr.`
    - User types 'draft number' -> Use property: `Draft No.`
    - User types 'draft round' -> Use property: `Draft Rd.`
    
    CRITICAL GENERATION RULES:
    1. Only return the raw Cypher query text inside your response. Do NOT wrap it in markdown code blocks like ```cypher.
    2. Your query MUST always return structural nodes in this exact format at the very end:
       ... RETURN elementId(u) AS id, properties(u) AS props
       
    3. BACKTICK PROPERTY ESCAPING:
       If a property key contains spaces, dots, or special characters (e.g., Draft Yr., Draft No., Draft Rd., FG%), 
       you MUST wrap the property name in backticks exactly as shown above so Cypher doesn't throw a syntax error.
       
    4. DATA TYPE SAFEGUARDS:

   - 'Games', 'Draft Yr.', 'Draft No.', 'Draft Rd.', and 'age' are stored
     as clean numbers. Write numeric comparisons directly. Do NOT use toInteger().

   - TEXT FIELD COMPARISON RULES:

    CRITICAL: OPERATOR SELECTION OVERRIDES

    The choice of comparison operator depends on the database property,
    not simply on whether the property contains text.

    - College = EXACT MATCH
    - Country = EXACT MATCH
    - Name = PARTIAL MATCH
    - Team = PARTIAL MATCH

    These rules override any general instruction about text matching.

    Never substitute CONTAINS for equality when searching College or Country.

     A. COLLEGE:
        - College searches MUST use EXACT equality.
        - Always use toLower() on both sides of the comparison.
        - NEVER use CONTAINS, STARTS WITH, or ENDS WITH for College.
        - Example:
          WHERE toLower(u.College) = toLower("Kentucky")

     B. COUNTRY:
        - Country searches MUST use EXACT equality.
        - Always use toLower() on both sides of the comparison.
        - NEVER use CONTAINS for Country.
        - Example:
          WHERE toLower(u.Country) = toLower("USA")

     C. NAME:
        - Use CONTAINS for partial name searches.
        - Always use toLower() for case-insensitive matching.
        - Example:
          WHERE toLower(u.Name) CONTAINS "michael jordan"

     D. TEAM:
        - Use CONTAINS for partial team searches when appropriate.
        - Always use toLower() for case-insensitive matching.
        - Example:
          WHERE toLower(u.Team) CONTAINS "lakers"

     E. STRICT ENFORCEMENT:
        - Never use CONTAINS on College or Country.
        - When the user provides a complete college or country name,
          ALWAYS use the equality operator (=).

    5. STRICT COMPARISON & VARIABLE ROADMAP:
       - NEVER nest a MATCH statement directly inside a WHERE clause. It causes a syntax error.
       - When comparing general players against a specific baseline player (e.g., "more games than Michael Jordan"):
         1. ALWAYS match the specific baseline player first using the variable 'm'.
         2. Extract ALL properties needed for the comparison using the WITH clause. Any property not explicitly passed or aliased in the WITH clause is lost.
            Example: WITH m.`FG%` AS mjFg, m.Assists AS mjAssists
         3. Match the general pool of players using the variable 'u'.
         4. Perform the filter using only the aliased variables from the WITH clause (e.g., WHERE u.Assists > mjAssists).
       - NEVER start a query with a general pool MATCH (u:Player) if a specific baseline player name is mentioned in the prompt.

    6. TOP, HIGHEST, AND MOST VALUE QUANTIFIERS:
       - When a user asks for "top X", "most X", or "highest X" (e.g., "10 players who played the most games"):
         1. You MUST append an ORDER BY clause targeting that property in descending order (e.g., ORDER BY u.Games DESC).
         2. You MUST append a LIMIT clause matching the requested count (e.g., LIMIT 10).
         3. ALWAYS filter out empty values in the WHERE clause first to protect data density (e.g., WHERE u.Games IS NOT NULL).

    7. SAME AS / RELATIONSHIP SELF-JOINS:
       - When a user asks to see nodes sharing a property with a specific entity (e.g., "players who played the same season as Aaron Gray"):
         1. You MUST first match the target anchor node in a distinct clause: MATCH (target:Player {Name: "Aaron Gray"})
         2. You MUST then match the global results using a shared property equation: MATCH (u:Player) WHERE u.Season = target.Season
         3. ALWAYS force the final return block to output all properties normally so the table loads safely:
            RETURN elementId(u) AS id, properties(u) AS props


    FEW-SHOT EXAMPLES:
    
    Question: Find players with more games and a higher field goal percentage than Michael Jordan
    Response:
    MATCH (m:Player) WHERE toLower(m.Name) CONTAINS "michael jordan" WITH m.`Games` AS mjGames, m.`FG%` AS mjFg MATCH (u:Player) WHERE u.`Games` > mjGames AND u.`FG%` > mjFg RETURN elementId(u) AS id, properties(u) AS props
    """



    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": f"Translate this request to Cypher: {user_prompt}"}
            ],
            temperature=0.0
        )
        # FIXED: Access choice 0 from the list first, then read its message content
        return response.choices[0].message.content.strip()

    except Exception as e:
        st.error(f"LLM Translation Error: {e}")
        return None

def load_filtered_players(cypher_query):
    import re

    # Reject empty or non-string queries
    if not isinstance(cypher_query, str) or not cypher_query.strip():
        st.error("GraphRAG rejected an empty query.")
        return pd.DataFrame()

    query = cypher_query.strip()

    # Basic safeguards against write operations and dangerous clauses
    blocked_keywords = [
        "CREATE",
        "MERGE",
        "DELETE",
        "DETACH",
        "SET",
        "REMOVE",
        "DROP",
        "ALTER",
        "GRANT",
        "DENY",
        "REVOKE",
        "LOAD CSV",
        "CALL",
        "FOREACH",
        "USE"
    ]

    # Reject multiple statements and Cypher comments
    if ";" in query or "//" in query or "/*" in query:
        st.error("GraphRAG rejected the query: unsupported syntax.")
        return pd.DataFrame()

    # Reject write-related or restricted keywords
    for keyword in blocked_keywords:
        pattern = r"\b" + re.escape(keyword) + r"\b"
        if re.search(pattern, query, re.IGNORECASE):
            st.error(
                f"GraphRAG rejected the query: "
                f"'{keyword}' is not permitted."
            )
            return pd.DataFrame()

    # Require a read query
    if not re.match(r"^\s*(MATCH|OPTIONAL\s+MATCH)\b", query, re.IGNORECASE):
        st.error("GraphRAG only permits MATCH-based read queries.")
        return pd.DataFrame()

    if not re.search(r"\bRETURN\b", query, re.IGNORECASE):
        st.error("GraphRAG query must contain RETURN.")
        return pd.DataFrame()

    try:
        st.sidebar.info("🤖 Executing Read-Only AI Cypher Statement:")
        st.sidebar.code(query, language="cypher")

        # Execute through a managed read transaction
        with driver.session() as session:
            records = session.execute_read(
                lambda tx: list(tx.run(query))
            )

        players = []

        for record in records:
            props = record["props"] or {}

            if isinstance(props, list):
                props = props[0] if props else {}
            elif not isinstance(props, dict):
                props = dict(props)

            players.append({"id": record["id"], **props})

        if not players:
            return pd.DataFrame()

        df_raw = pd.DataFrame(players)

        # Normalize property names
        df_raw.columns = [
            c.lower() if c not in ["id", "Select"] else c
            for c in df_raw.columns
        ]

        # Remove duplicate columns
        df_raw = df_raw.loc[:, ~df_raw.columns.duplicated()]

        return normalize_dataframe_data(df_raw)

    except Exception as e:
        st.error(f"⚠️ Neo4j Read-Only Query Error: {e}")
        return pd.DataFrame()



# --- STEP 1: INITIALIZE DATA FIRST (At the root indentation level) ---
# FIXED: Changed bracket syntax to .get() to permanently eliminate the KeyError crash
if st.session_state.get("rag_query"):
    df = load_filtered_players(st.session_state["rag_query"])
else:
    df = load_players()

if df.empty:
    st.warning("No player matches found.")
    
    # Render the input controls even when empty so the user can change or reset their query
    sort_c1, sort_c2, sort_c3 = st.columns([1.5, 1.5, 3.0])
    with sort_c3:
        user_question = st.text_area("🔍 GraphRAG AI Filter", placeholder="Ask a question...", height=80)
        rag_btn1, rag_btn2 = st.columns(2)
        with rag_btn1:
            if st.button("🚀 Run Filter", use_container_width=True) and user_question:
                generated_cypher = translate_text_to_cypher(user_question)
                if generated_cypher:
                    st.session_state["rag_query"] = generated_cypher
                    st.session_state.start_idx = 0
                    st.rerun()
        with rag_btn2:
            if st.button("🔄 Reset View", use_container_width=True):
                st.session_state["rag_query"] = None
                st.session_state.start_idx = 0
                st.rerun()
else:
    if "Select" not in df.columns: 
        df.insert(0, "Select", False)

    # Global Table Column Ordering Matrix
    def table_column_sort_key(col_name):
        if col_name == "Select": return -1
        if col_name == "id": return 999
        if col_name.lower() in PLAYER_FIELD_ORDER: return PLAYER_FIELD_ORDER.index(col_name.lower())
        return len(PLAYER_FIELD_ORDER) + 1

    df = df[sorted(df.columns, key=table_column_sort_key)]
    available_columns = [c for c in df.columns if c not in ["id", "Select"]]

    # --- STEP 2: NOW DRAW YOUR CONTROLS ROW USING THE DATA GENERATED ABOVE ---
    sort_c1, sort_c2, sort_c3 = st.columns([1.5, 1.5, 3.0])

    with sort_c1:
        # FIX: Changed variable name to selected_col to match your line 679
        selected_col = st.selectbox(
            "Select Column to Sort By",
            options=list(dict.fromkeys([str(col).lower() for col in df.columns if col not in ["id", "Select"]])),
            index=0
        )


    with sort_c2:
        direction = st.radio("Direction", options=["Ascending", "Descending"], horizontal=True)
        is_ascending = (direction == "Ascending")

    with sort_c3:
        user_question = st.text_area(
            "🔍 GraphRAG AI Filter", 
            placeholder="Ask a question (e.g., Show me players over 30 who play for LAL)", 
            height=80,
            value=st.session_state.get("user_last_question", "") # Optional: retains question text
        )
            
        rag_btn1, rag_btn2 = st.columns(2)
        with rag_btn1:
            if st.button("🚀 Run Filter", use_container_width=True) and user_question:
                with st.spinner("Generating Cypher Query..."):
                    generated_cypher = translate_text_to_cypher(user_question)
                    if generated_cypher:
                        st.session_state["rag_query"] = generated_cypher
                        st.session_state["user_last_question"] = user_question
                        st.session_state.start_idx = 0
                        st.rerun()
        with rag_btn2:
            if st.button("🔄 Reset View", use_container_width=True):
                st.session_state["rag_query"] = None
                st.session_state["user_last_question"] = ""
                st.session_state.start_idx = 0
                st.rerun()

    # --- STEP 3: RUN YOUR ROW SORTING BLOCKS ---
    # FIXED: This now runs cleanly entirely inside the active data else wrapper block
    new_sort_col = None if selected_col == "None" else selected_col
    if new_sort_col != st.session_state.sort_col or is_ascending != st.session_state.sort_asc:
        st.session_state.sort_col = new_sort_col
        st.session_state.sort_asc = is_ascending
        st.session_state.start_idx = 0  
        st.rerun()


    # 1. NUCLEAR CLEANUP: Rebuild columns as completely unique strings right at the source
    df.columns = [str(c).strip().lower() for c in df.columns]
    
    # 2. FORCE PHYSICAL DEDUPLICATION: Break any hidden multi-index or duplicate structures
    df = df.loc[:, ~df.columns.duplicated(keep='first')].copy()
    
    # 3. Align session state to match the fresh lowercase columns
    if st.session_state.get("sort_col"):
        st.session_state.sort_col = str(st.session_state.sort_col).strip().lower()

    # 4. EXECUTE BULLETPROOF SORT
    if st.session_state.sort_col and st.session_state.sort_col in df.columns:
        # We extract the target column explicitly as a standard 1D array to bypass Pandas loops entirely
        sort_target = df[st.session_state.sort_col]
        if hasattr(sort_target, 'ndim') and sort_target.ndim > 1:
            sort_target = sort_target.iloc[:, 0]  # Hard extract the absolute first match if columns still ghost split
            
        def natural_sort_key(series):
            def clean_val(val):
                if pd.isna(val) or str(val).strip() == "" or str(val).lower() == "nan": 
                    return "~~~~"
                try: 
                    return f"{float(val):012.3f}"
                except (ValueError, TypeError): 
                    return str(val).lower().strip()
            return series.apply(clean_val)
            
        # We assign a completely fresh index map to prevent index collision crashes
        sort_order = natural_sort_key(sort_target)
        df['__sort_idx__'] = sort_order
        df = df.sort_values(by='__sort_idx__', ascending=st.session_state.sort_asc).drop(columns=['__sort_idx__']).reset_index(drop=True)


    if "Select" not in df.columns:
        df.insert(0, "Select", False)

        # --- FIXED: Global Table Column Ordering Layer ---
        # Define how we want columns arranged: 'Select' first, then PLAYER_FIELD_ORDER, then anything else
        def table_column_sort_key(col_name):
            if col_name == "Select":
                return -1  # Always force the selection checkbox to the far left
            if col_name == "id":
                return 999  # Keep hidden ID tracking keys at the very end out of the way
            if col_name.lower() in PLAYER_FIELD_ORDER:
                return PLAYER_FIELD_ORDER.index(col_name.lower())
            return len(PLAYER_FIELD_ORDER) + 1  # Append extra dynamic properties at the end

        # Re-index the dataframe columns based on our clean layout weights list
        sorted_columns = sorted(df.columns, key=table_column_sort_key)
        df = df[sorted_columns]

        # --- Global Sorting Layer (by rows) ---
        available_columns = [c for c in df.columns if c not in ["id", "Select"]]

    # --- Pagination Slicing Layer ---
    total_players = len(df)
    PAGE_SIZE = 15
    FAST_FORWARD_SIZE = 150

    if st.session_state.start_idx >= total_players:
        st.session_state.start_idx = max(0, total_players - PAGE_SIZE)

    current_start = st.session_state.start_idx
    current_end = min(current_start + PAGE_SIZE, total_players)
    # FIX: Check if a user clicked a KPI link, text search, or graph node to filter the data
    if st.session_state.get("search_filter", "") != "":
        current_filter = str(st.session_state.search_filter).strip().lower()
        
        # 1. Base exact matches for Name and College
        name_match = (df['name'].str.lower().str.strip() == current_filter).fillna(False)
        college_match = (df['college'].str.lower().str.strip() == current_filter).fillna(False)
        combined_match = name_match | college_match
        
        # 2. Match for Team abbreviations or names
        if "team" in df.columns:
            team_match = df['team'].str.lower().str.strip().str.contains(current_filter, na=False).fillna(False)
            combined_match = combined_match | team_match
        
        # 3. Numeric match if the user clicked the "Most Games" KPI metric
        if current_filter.isdigit():
            games_match = (df['games'] == int(current_filter)).fillna(False)
            combined_match = combined_match | games_match
            
        # 4. Text match if the user searched for a Season timeline
        elif "season" in df.columns and ("-" in current_filter or (len(current_filter) == 4 and current_filter.isdigit())):
            season_match = df['season'].astype(str).str.lower().str.contains(current_filter, na=False).fillna(False)
            combined_match = combined_match | season_match
        
        # Execute the unified match filter safely
        df_filtered = df[combined_match].copy()
        
        # --- CRITICAL FIX: AUTOMATICALLY TICK THE CHECKBOX FOR A SINGLE FILTERED PLAYER ---
        if len(df_filtered) == 1 and "select" in df_filtered.columns:
            df_filtered.loc[:, "select"] = True
            
            # Extract the unique database hash ID for this isolated player row
            target_id = df_filtered.iloc[0].get("id")
            if target_id:
                # Forcefully update the global session checklist so the network graph loops can read it safely
                st.session_state["selected_player_ids"] = [target_id]

        # Display the clear reset button above the data grid
        if st.button("❌ Clear Filter and Show All Players", type="secondary"):
            st.session_state.search_filter = ""
            st.rerun()
            
        total_players = len(df_filtered)
        current_end = min(current_start + PAGE_SIZE, total_players)
        sliced_df = df_filtered.iloc[current_start:current_end].copy()
    else:
        sliced_df = df.iloc[current_start:current_end].copy()


    st.caption(f"Showing page **{(current_start // PAGE_SIZE) + 1}** of **{(total_players + PAGE_SIZE - 1) // PAGE_SIZE}** (Players {current_start + 1} to {current_end} out of {total_players})")

    # --- Dynamic Table View Configuration ---
    # Start with your strict structural column defaults (all keys forced to lowercase)
    col_config = {
        "id": None,
        "select": st.column_config.CheckboxColumn("Select", help="Check row to activate Edit/Delete actions", default=False)
    }  
    
    # Track if we have already configured the checkbox column once
    has_checkbox_assigned = False
    
    # Dynamically build a configuration layout for EVERY active column in your DataFrame
    for col in df.columns:
        norm_col = str(col).lower().strip()
        
        # Skip hardcoded system columns we already defined above
        if norm_col == "id":
            continue
            
        # FIX: COMPLETELY HIDE OUR MANUAL SELECT COLUMN SO IT DOES NOT DUPLICATE ON THE RIGHT
        # elif norm_col == "select":
        #     col_config[col] = None

        elif norm_col == "select":
            col_config[col] = st.column_config.CheckboxColumn(
                "Select",
                help="Check row to activate Edit/Delete actions",
                default=False
            )
            
        # Assign special cell pickers for your dropdown fields
        elif norm_col == "state":
            col_config[col] = st.column_config.SelectboxColumn("US State", options=us_states)
        elif norm_col == "country":  
            col_config[col] = st.column_config.SelectboxColumn("Country", options=world_countries)
        elif norm_col == "team":  
            col_config[col] = st.column_config.SelectboxColumn("Team", options=nba_dropdown_options)
            
        # Automatically format stats and percentage columns to render cleanly
        elif norm_col.endswith("%") or norm_col in ["fg%", "drb%", "orb%", "ast%", "usg%"]:
            col_config[col] = st.column_config.NumberColumn(col.title(), format="%.4f")
        elif norm_col in ["age", "games", "draft yr.", "draft no.", "draft rd.", "reb", "ast", "net", "orb"]:
            col_config[col] = st.column_config.NumberColumn(col.title(), format="%d")
            
        # Fallback: Safely render all standard text fields (Name, Season, College) so data shows up
        else:
            col_config[col] = st.column_config.Column(col.title())



    # Generate your dynamic state rendering key cleanly
    dynamic_key = f"player_editor_{current_start}_{st.session_state.sort_col}_{st.session_state.sort_asc}"
    st.session_state.dynamic_key = dynamic_key

    # --- Dynamic Table View Configuration ---
    col_config = {
        "id": None,
        "Select": st.column_config.CheckboxColumn("Select", help="Check row to activate Edit/Delete actions", default=False)
    }  
    if "state" in df.columns:
        col_config["state"] = st.column_config.SelectboxColumn("US State", options=us_states)
        
    # ADD THIS LINE: Locks the country column in the spreadsheet to your valid dropdown list too
    if "country" in df.columns:  
        col_config["country"] = st.column_config.SelectboxColumn("Country", options=world_countries)

    # ADD THIS LINE: Locks the team column in the spreadsheet to your valid dropdown list too
    if "team" in df.columns:
        col_config["team"] = st.column_config.SelectboxColumn("Team", options=nba_dropdown_options)


    # --- Render Table Workspace ---
    # 1. Keep only the first occurrence of each column name and drop the copies
    cleaned_sliced_df = sliced_df.loc[:, ~sliced_df.columns.duplicated()]
    lowercase_col_config = {str(k).lower(): v for k, v in col_config.items()}

    # Inject custom CSS to make the KPI font size smaller and change text colors to Dark Blue
    st.markdown(
        """
        <style>
        /* 1. Sets your standard non-clickable KPI metrics to Dark Blue */
        [data-testid="stMetricValue"] { 
            font-size: 28px !important; 
            color: #003366 !important; /* Dark Blue Hex Code */
        }
        
        /* Keeps your KPI header labels the exact same grey color format */
        [data-testid="stMetricLabel"] p { 
            font-size: 13px !important; 
        }
        
        /* 2. TARGET COLUMNS 3, 5, AND 6 AND COMPLETELY REMOVE BUTTON BORDERS/BACKGROUNDS */
        div[data-testid="stGridColumn"]:nth-child(5) button,
        div[data-testid="stGridColumn"]:nth-child(6) button {
            background-color: transparent !important;
            border: none !important;
            color: #003366 !important; /* Dark Blue Hex Code for Clickable Metrics */
            font-size: 28px !important;
            font-weight: bold !important;
            padding: 0px !important;
            text-align: left !important;
            box-shadow: none !important;
            height: auto !important;
            width: auto !important;
            min-height: unset !important;
        }
        
        /* FORCE THE TEXT TO TURN SMOOTH SKY BLUE ONLY ON MOUSE HOVER */
        div[data-testid="stGridColumn"]:nth-child(5) button:hover,
        div[data-testid="stGridColumn"]:nth-child(6) button:hover {
            color: #0066CC !important;
            text-decoration: underline !important;
            background-color: transparent !important;
        }
        </style>
        """,
        unsafe_allow_html=True
    )


    # Force the 'age' and 'games' columns to be entirely numeric before processing calculations
    df['age'] = pd.to_numeric(df['age'], errors='coerce')
    df['games'] = pd.to_numeric(df['games'], errors='coerce')

    # Inject custom CSS to make the KPI font size smaller and more compact
    st.markdown(
        """
        <style>
        [data-testid="stMetricValue"] { font-size: 24px !important; }
        [data-testid="stMetricLabel"] p { font-size: 13px !important; }
        </style>
        """,
        unsafe_allow_html=True
    )

    # FIX: Expand layout grid allocations from 5 to 6 equal metric columns
    kpi_c1, kpi_c2, kpi_c3, kpi_c4, kpi_c5, kpi_c6 = st.columns(6)
    
        # --- ELEMENT 1: Total Players (Now Using the Exact Same Font and Color) ---
    with kpi_c1:
        total_players_count = len(df)
        
        # 1. Clean HTML Title Label matching your other cards
        st.markdown("<p style='font-size: 13px; color: gray; margin-bottom: 2px; font-family: Arial, sans-serif;'>Total Players</p>", unsafe_allow_html=True)
        
        # 2. MATCHING PURE HTML TEXT: Uses identical sizing, weights, and dark blue styling
        st.markdown(
            f"""
            <span style='
                font-size: 24px; 
                font-weight: bold; 
                color: #003366; 
                font-family: Arial, sans-serif;
                display: inline-block;
            '>
                {total_players_count}
            </span>
            """,
            unsafe_allow_html=True
        )

        
    # --- ELEMENT 2: Avg Age (Now Using the Exact Same Font and Color) ---
    with kpi_c2:
        avg_age = df['age'].mean(skipna=True)
        
        # 1. Clean HTML Title Label matching your other cards
        st.markdown("<p style='font-size: 13px; color: gray; margin-bottom: 2px; font-family: Arial, sans-serif;'>Avg Age</p>", unsafe_allow_html=True)
        
        if pd.notna(avg_age):
            age_display = f"{avg_age:.1f}"
            
            # 2. MATCHING PURE HTML TEXT: Uses identical sizing, weights, and dark blue styling as the clickable cards
            st.markdown(
                f"""
                <span style='
                    font-size: 24px; 
                    font-weight: bold; 
                    color: #003366; 
                    font-family: Arial, sans-serif;
                    display: inline-block;
                '>
                    {age_display}
                </span>
                """,
                unsafe_allow_html=True
            )
        else:
            st.markdown("<p style='font-size: 24px; font-weight: bold; color: gray;'>N/A</p>", unsafe_allow_html=True)

        
    # --- ELEMENT 3: Top College (Pure Clickable Text Link - Dark Blue) ---
    with kpi_c3:
        if st.query_params.get("click_college") == "true":
            st.query_params.clear()
            valid_colleges = df['college'].dropna()
            valid_colleges = valid_colleges[valid_colleges.astype(str).str.strip().str.lower() != "nan"]
            valid_colleges = valid_colleges[valid_colleges.astype(str).str.strip() != ""]
            valid_colleges = valid_colleges[valid_colleges.astype(str).str.strip().str.lower() != "none"]
            if not valid_colleges.empty:
                mode_series = valid_colleges.mode()
                if not mode_series.empty:
                    st.session_state.search_filter = str(mode_series.iloc[0]).lower().strip()
                    st.rerun()

        valid_colleges = df['college'].dropna()
        valid_colleges = valid_colleges[valid_colleges.astype(str).str.strip().str.lower() != "nan"]
        valid_colleges = valid_colleges[valid_colleges.astype(str).str.strip() != ""]
        valid_colleges = valid_colleges[valid_colleges.astype(str).str.strip().str.lower() != "none"]
        
        st.markdown("<p style='font-size: 13px; color: gray; margin-bottom: 2px; font-family: Arial, sans-serif;'>Top College</p>", unsafe_allow_html=True)
        
        if not valid_colleges.empty:
            mode_series = valid_colleges.mode()
            if not mode_series.empty:
                college_name = str(mode_series.iloc[0]).title()
                st.markdown(
                    f"""
                    <a href='?click_college=true' target='_self' style='
                        font-size: 24px; font-weight: bold; color: #003366; 
                        text-decoration: none; font-family: Arial, sans-serif; display: inline-block;
                    ' onmouseover=\"this.style.color='#0066CC'; this.style.textDecoration='underline';\" 
                      onmouseout=\"this.style.color='#003366'; this.style.textDecoration='none';\">
                        {college_name}
                    </a>
                    """,
                    unsafe_allow_html=True
                )
            else:
                st.markdown("<p style='font-size: 24px; font-weight: bold; color: gray;'>N/A</p>", unsafe_allow_html=True)
        else:
            st.markdown("<p style='font-size: 24px; font-weight: bold; color: gray;'>N/A</p>", unsafe_allow_html=True)


            
        # --- ELEMENT 4: Most Games (Pure Clickable Text Link - Dark Blue) ---
    with kpi_c4:
        if st.query_params.get("click_games") == "true":
            st.query_params.clear()
            max_games_val = df['games'].max(skipna=True)
            if pd.notna(max_games_val):
                st.session_state.search_filter = str(int(max_games_val)).strip()
                st.rerun()

        max_games = df['games'].max(skipna=True)
        st.markdown("<p style='font-size: 13px; color: gray; margin-bottom: 2px; font-family: Arial, sans-serif;'>Most Games</p>", unsafe_allow_html=True)
        
        if pd.notna(max_games):
            games_display = str(int(max_games))
            st.markdown(
                f"""
                <a href='?click_games=true' target='_self' style='
                    font-size: 24px; font-weight: bold; color: #003366; 
                    text-decoration: none; font-family: Arial, sans-serif; display: inline-block;
                ' onmouseover=\"this.style.color='#0066CC'; this.style.textDecoration='underline';\" 
                  onmouseout=\"this.style.color='#003366'; this.style.textDecoration='none';\">
                    {games_display}
                </a>
                """,
                unsafe_allow_html=True
            )
        else:
            st.markdown("<p style='font-size: 24px; font-weight: bold; color: gray;'>N/A</p>", unsafe_allow_html=True)


        # --- ELEMENT 5: Top Vet Leader (Pure Clickable Text Link - Dark Blue) ---
    with kpi_c5:
        if "search_filter" not in st.session_state:
            st.session_state.search_filter = ""

        if st.query_params.get("click_leader") == "true":
            st.query_params.clear()
            valid_leaders = df.dropna(subset=['games', 'age', 'name'])
            if not valid_leaders.empty:
                top_player_row = valid_leaders.sort_values(by=['games', 'age'], ascending=[False, False]).iloc[0]
                st.session_state.search_filter = str(top_player_row['name']).lower().strip()
                st.rerun()

        valid_leaders = df.dropna(subset=['games', 'age', 'name'])
        if not valid_leaders.empty:
            top_player_row = valid_leaders.sort_values(by=['games', 'age'], ascending=[False, False]).iloc[0]
            leader_name = str(top_player_row['name']).title()
            
            st.markdown("<p style='font-size: 13px; color: gray; margin-bottom: 2px; font-family: Arial, sans-serif;'>Top Vet Leader</p>", unsafe_allow_html=True)
            st.markdown(
                f"""
                <a href='?click_leader=true' target='_self' style='
                    font-size: 24px; font-weight: bold; color: #003366; 
                    text-decoration: none; font-family: Arial, sans-serif; display: inline-block;
                ' onmouseover=\"this.style.color='#0066CC'; this.style.textDecoration='underline';\" 
                  onmouseout=\"this.style.color='#003366'; this.style.textDecoration='none';\">
                    {leader_name}
                </a>
                """,
                unsafe_allow_html=True
            )
        else:
            st.markdown("<p style='font-size: 13px; color: gray; margin-bottom: 2px;'>Top Vet Leader</p>", unsafe_allow_html=True)
            st.markdown("<p style='font-size: 24px; font-weight: bold; color: gray;'>N/A</p>", unsafe_allow_html=True)


        # --- ELEMENT 6: Top Overall Performer (Pure Clickable Text Link - Dark Blue) ---
    with kpi_c6:
        if st.query_params.get("click_performer") == "true":
            st.query_params.clear()
            for stat_col in ['games', 'reb', 'ast', 'fg%', 'net']:
                if stat_col in df.columns:
                    df[stat_col] = pd.to_numeric(df[stat_col], errors='coerce')
            calc_df = df.copy()
            calc_df['reb'] = calc_df['reb'].fillna(0)
            calc_df['ast'] = calc_df['ast'].fillna(0)
            calc_df['games'] = calc_df['games'].fillna(0)
            calc_df['fg%'] = calc_df['fg%'].fillna(0)
            calc_df['net'] = calc_df['net'].fillna(0)
            calc_df['stat_score'] = (calc_df['reb']*1.2)+(calc_df['ast']*1.5)+(calc_df['net']*2.0)+(calc_df['games']*0.5)+(calc_df['fg%']*10.0)
            valid_stats = calc_df[calc_df['name'].notna() & (calc_df['name'].str.strip() != "") & (calc_df['games'] >= 70)]
            if not valid_stats.empty and calc_df['stat_score'].max() > 0:
                top_stat_row = valid_stats.sort_values(by='stat_score', ascending=False).iloc[0]
                st.session_state.search_filter = str(top_stat_row['name']).lower().strip()
                st.rerun()

        for stat_col in ['games', 'reb', 'ast', 'fg%', 'net']:
            if stat_col in df.columns:
                df[stat_col] = pd.to_numeric(df[stat_col], errors='coerce')
        
        calc_df = df.copy()
        calc_df['reb'] = calc_df['reb'].fillna(0)
        calc_df['ast'] = calc_df['ast'].fillna(0)
        calc_df['games'] = calc_df['games'].fillna(0)
        calc_df['fg%'] = calc_df['fg%'].fillna(0)
        calc_df['net'] = calc_df['net'].fillna(0)
        calc_df['stat_score'] = (calc_df['reb']*1.2)+(calc_df['ast']*1.5)+(calc_df['net']*2.0)+(calc_df['games']*0.5)+(calc_df['fg%']*10.0)
        valid_stats = calc_df[calc_df['name'].notna() & (calc_df['name'].str.strip() != "") & (calc_df['games'] >= 80)]
        
        if not valid_stats.empty and calc_df['stat_score'].max() > 0:
            top_stat_row = valid_stats.sort_values(by='stat_score', ascending=False).iloc[0]
            stat_leader_name = str(top_stat_row['name']).title()
            
            st.markdown("<p style='font-size: 13px; color: gray; margin-bottom: 2px; font-family: Arial, sans-serif;'>Top Overall Performer</p>", unsafe_allow_html=True)
            st.markdown(
                f"""
                <a href='?click_performer=true' target='_self' style='
                    font-size: 24px; font-weight: bold; color: #003366; 
                    text-decoration: none; font-family: Arial, sans-serif; display: inline-block;
                ' onmouseover=\"this.style.color='#0066CC'; this.style.textDecoration='underline';\" 
                  onmouseout=\"this.style.color='#003366'; this.style.textDecoration='none';\">
                    {stat_leader_name}
                </a>
                """,
                unsafe_allow_html=True
            )
        else:
            st.markdown("<p style='font-size: 13px; color: gray; margin-bottom: 2px;'>Top Overall Performer</p>", unsafe_allow_html=True)
            st.markdown("<p style='font-size: 24px; font-weight: bold; color: gray;'>N/A</p>", unsafe_allow_html=True)


    # --- Render Table Workspace ---
    # 1. FIX: Harmonize all column casings to absolute lowercase right before the duplicate check
    sliced_df.columns = [str(c).lower().strip() for c in sliced_df.columns]
    
    # 2. Keep only the first occurrence of each column name and permanently drop the extra copy
    cleaned_sliced_df = sliced_df.loc[:, ~sliced_df.columns.duplicated()].copy()

    # 3. Move the single remaining "select" column to the absolute front of the table layout grid
    if "select" in cleaned_sliced_df.columns:
        cols_ordered = ["select"] + [c for c in cleaned_sliced_df.columns if c != "select"]
        cleaned_sliced_df = cleaned_sliced_df[cols_ordered]

    lowercase_col_config = {str(k).lower(): v for k, v in col_config.items()}

    # Check row selection changes via data_editor 
    edited_df = st.data_editor(
        cleaned_sliced_df,  
        key=st.session_state.dynamic_key,
        column_config=lowercase_col_config,  
        hide_index=True,
        height=570,
        use_container_width=True
    )


    # 2. FIX: Capture ALL checkbox clicks from Streamlit's editor memory block into a list
    selected_player_ids = []
    selected_player_props = {}

    # Extract edit dictionary history from the data_editor state
    editor_state = st.session_state.get(st.session_state.dynamic_key, {})
    edited_rows = editor_state.get("edited_rows", {})

    for row_idx_str, changes in edited_rows.items():
        is_checked = changes.get("select") is True or changes.get("Select") is True
        
        if is_checked:
            row_idx = int(row_idx_str)
            if row_idx < len(cleaned_sliced_df):
                selected_row_data = cleaned_sliced_df.iloc[row_idx]
                p_id = selected_row_data.get("id")
                selected_player_ids.append(p_id)
                
                # Keep track of props for the single-edit fallback
                if len(selected_player_ids) == 1:
                    selected_player_props = selected_row_data.to_dict()

    # --- Navigation Panel & Unified Bottom Control Row ---
    st.markdown("---")
    ctrl_cols = st.columns([1.2, 1.2, 1.4, 2.0, 1.2, 1.2, 1.2, 1.2])
    
    # 3. FIX: Establish your precise conditional button states based on selection count
    selection_count = len(selected_player_ids)
    
    disable_add = selection_count > 0        # Disabled if 1 or more selections are made
    disable_edit = selection_count != 1      # Disabled if 0 OR if 2 or more selections are made
    disable_delete = selection_count == 0    # Disabled if 0 selections are made (allows multi-delete if you expand later)

    # --- LEFT SIDE: Action Buttons ---
    with ctrl_cols[0]:
        # Button 1: Add (Disabled if any row is checked)
        if st.button("➕ Add", disabled=disable_add, use_container_width=True):
            base_cols = [c for c in df.columns if c not in ["id", "select"]]
            add_player_dialog(base_cols)

    with ctrl_cols[1]:
        # Button 2: Edit (Only enabled if EXACTLY 1 row is checked)
        if st.button("📝 Edit", type="primary", disabled=disable_edit, use_container_width=True):
            st.session_state[f"modal_view_{selected_player_ids[0]}"] = "EDIT"
            edit_player_dialog(selected_player_ids[0], selected_player_props)

    with ctrl_cols[2]:
        # Button 3: Delete (Enabled if 1 or more rows are checked)
        if st.button("🗑️ Delete", type="secondary", disabled=disable_delete, use_container_width=True):
            confirm_delete_dialog(selected_player_ids)


    # Note: ctrl_cols[3] is intentionally left blank as a flexible gap spacer

    # --- RIGHT SIDE: Navigation Arrows ---
    with ctrl_cols[4]:
        if st.button("⏮ Back 10 Pages", disabled=(current_start == 0), use_container_width=True):
            st.session_state.start_idx = max(0, current_start - FAST_FORWARD_SIZE)
            st.rerun()

    with ctrl_cols[5]:
        if st.button("◀ Previous Page", disabled=(current_start == 0), use_container_width=True):
            st.session_state.start_idx = max(0, current_start - PAGE_SIZE)
            st.rerun()

    with ctrl_cols[6]:
        if st.button("Next Page ▶", disabled=(current_end >= total_players), use_container_width=True):
            st.session_state.start_idx = min(total_players - 1, current_start + PAGE_SIZE)
            st.rerun()

    with ctrl_cols[7]:
        max_possible_start = ((total_players - 1) // PAGE_SIZE) * PAGE_SIZE
        if st.button("Forward 10 Pages ⏭", disabled=(current_end >= total_players), use_container_width=True):
            st.session_state.start_idx = min(max_possible_start, current_start + FAST_FORWARD_SIZE)
            st.rerun()


        # --- Step 4: CENTERING THE GLOBAL RELATIONSHIP NETWORK GRAPH WHEEL ---
    if len(selected_player_ids) == 1:
        st.markdown("---")
        
        # 1. Section Title Header Placement
        st.markdown("<h3 style='text-align: center; margin-bottom: 5px;'>🕸️ Player Network Relationship Graph Ecosystem</h3>", unsafe_allow_html=True)
        
        # Pull the player's core attributes cleanly from your active table row dictionary
        player_row = next(
            (row for _, row in cleaned_sliced_df.iterrows() if row.get("id") in selected_player_ids), 
            None
        )
        
        if player_row is not None:
            nodes = []
            edges = []
            
            # Rebuild the graph structural node mapping first so agraph has its payload ready
            main_name = str(player_row.get("name", "Selected Player")).title()
            nodes.append(Node(id=main_name, label=main_name, size=45, shape="circle", color="#003366", font={'color': 'white', 'size': 13, 'face': 'Arial'}))
            
            raw_team = player_row.get("team", "")
            if pd.notna(raw_team) and str(raw_team).strip() != "":
                team_abbr = str(raw_team).split(" - ").pop(0).strip().upper()
                nodes.append(Node(id=team_abbr, label=team_abbr, size=35, shape="circle", color="#0066CC", font={'color': 'white', 'size': 12, 'face': 'Arial'}))
                edges.append(Edge(source=main_name, target=team_abbr, color="#0066CC", width=3, dashed=True, label="PLAYED FOR", font={'color': 'black', 'size': 11, 'align': 'bottom'}))
                
                with driver.session() as session:
                    teammate_query = "MATCH (p:Player) WHERE (p.Team = $raw_team OR p.Team STARTS WITH $team_abbr) AND p.Name <> $main_name RETURN p.Name AS teammate_name LIMIT 12"
                    global_teammates = session.run(teammate_query, raw_team=raw_team, team_abbr=team_abbr, main_name=player_row.get("name"))
                    for record in global_teammates:
                        mate_name = str(record["teammate_name"]).title()
                        if mate_name not in [n.id for n in nodes]:
                            nodes.append(Node(id=mate_name, label=mate_name, size=15, color="#A6C8FF"))
                            edges.append(Edge(source=team_abbr, target=mate_name, color="#A6C8FF", width=1, label="PLAYED WITH", font={'color': 'black', 'size': 9, 'align': 'bottom'}))
            
            raw_college = player_row.get("college", "")
            if pd.notna(raw_college) and str(raw_college).strip() != "" and str(raw_college).lower() != "nan":
                college_node_name = str(raw_college).title()
                nodes.append(Node(id=college_node_name, label=college_node_name, size=35, shape="circle", color="#00CC66", font={'color': 'white', 'size': 12, 'face': 'Arial'}))
                edges.append(Edge(source=main_name, target=college_node_name, color="#00CC66", width=3, dashed=True, label="ALUMNI OF", font={'color': 'black', 'size': 11, 'align': 'bottom'}))
                
                with driver.session() as session:
                    alumni_query = "MATCH (p:Player) WHERE p.College = $raw_college AND p.Name <> $main_name RETURN p.Name AS alumni_name LIMIT 12"
                    global_alumni = session.run(alumni_query, raw_college=raw_college, main_name=player_row.get("name"))
                    for record in global_alumni:
                        alum_name = str(record["alumni_name"]).title()
                        if alum_name not in [n.id for n in nodes]:
                            nodes.append(Node(id=alum_name, label=alum_name, size=15, color="#B2F2BB"))
                            edges.append(Edge(source=college_node_name, target=alum_name, color="#B2F2BB", width=1, label="ATTENDED", font={'color': 'black', 'size': 9, 'align': 'bottom'}))

            
            # Initialize the canvas viewport config ahead of the button tracking run
            config = Config(
                width=1400,
                height=550,
                directed=False,
                physics=True,
                hierarchical=False,
                nodeHighlightBehavior=True,
                highlightColor="#F77737",
                collapsible=False
            )

            # 2. Capture whichever node is actively selected or clicked on
            selected_node_raw = agraph(nodes=nodes, edges=edges, config=config)

            if selected_node_raw is not None:
                clicked_clean = str(selected_node_raw).strip()

                if clicked_clean.startswith("🎓 "):
                    clicked_clean = clicked_clean.replace("🎓 ", "")
                elif clicked_clean.startswith("🏀 "):
                    clicked_clean = clicked_clean.replace("🏀 ", "")

                display_name = clicked_clean.upper() if clicked_clean.isupper() else clicked_clean.title()

                st.write("")

                pad_l, btn_core_container, pad_r = st.columns([5.0, 2.0, 5.0])

                with btn_core_container:
                    if st.button(
                        f"⚡ Filter for: {display_name}",
                        type="secondary",
                        use_container_width=True,
                        help=f"Click to slice the data table grid down to {display_name}"
                    ):
                        st.session_state.search_filter = display_name.lower()
                        st.rerun()

            else:
                st.markdown(
                    "<p style='text-align: center; font-size: 12px; color: #9da5b1; margin-bottom: 5px; font-style: italic;'>Click any graph bubble node below to filter the table</p>",
                    unsafe_allow_html=True
                )
         
    # --- CLEAR THE GRAPH COMPLETELY WHEN 0 ROWS ARE SELECTED ---
    else:
        st.empty()


    # --- Background Spreadsheet Auto-Save Engine ---
    has_real_edits = False
    
    # Extract the inline cell updates safely before the saving loop runs
    editor_state_data = st.session_state.get(st.session_state.dynamic_key, {})
    edited_rows_dict = editor_state_data.get("edited_rows", {})

    for row_key, field_changes in edited_rows_dict.items():
        # Check for 'select' case-insensitively to properly catch checkbox unselect events
        non_select_changes = {k: v for k, v in field_changes.items() if str(k).lower().strip() != "select"}
        if non_select_changes:
            has_real_edits = True
            break
            
    if has_real_edits:
        with driver.session() as session:
            for row_index_str, updated_fields in edited_rows_dict.items():
                # Filter out the checkbox changes so they are not saved as empty data
                clean_fields = {k: v for k, v in updated_fields.items() if str(k).lower().strip() != "select"}
                if not clean_fields:
                    continue
                    
                row_idx = int(row_index_str)
                player_id = sliced_df.iloc[row_idx]["id"]
                current_props = sliced_df.iloc[row_idx].drop(labels=["id", "select"], errors="ignore").to_dict()

                current_props.update(clean_fields)
                
                clean_props = {k: v for k, v in current_props.items() if pd.notna(v) and v != ""}
                session.execute_write(update_user_node, player_id, clean_props)
        st.success("Successfully pushed inline updates to Neo4j!")
        st.rerun()

        # --- SPREADSHEET VIEW (iOS/Safari Safe Fallback) ---
        st.subheader("Raw Player Database Records")

        if not df_players.empty:
            # Using st.table instead of st.dataframe bypasses the JavaScript module failure 
            # and renders your data as a clean, reliable HTML grid on mobile devices.
            st.table(df_players)
        else:
            st.warning("No tabular player records found to display.")

