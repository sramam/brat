#!/usr/bin/env python
# -*- Mode: Python; tab-width: 4; indent-tabs-mode: nil; coding: utf-8; -*-
# vim:set ft=python ts=4 sw=4 sts=4 autoindent:


'''
Per-project configuration functionality for
Brat Rapid Annotation Tool (brat)
'''

import re

from annotation import open_textfile
from message import Messager

# TODO: replace with reading a proper ontology.

class InvalidProjectConfigException(Exception):
    pass

# config section name constants
ENTITY_SECTION    = "entities"
RELATION_SECTION  = "relations"
EVENT_SECTION     = "events"
ATTRIBUTE_SECTION = "attributes"

expected_configuration_sections = (ENTITY_SECTION, RELATION_SECTION, EVENT_SECTION, ATTRIBUTE_SECTION)

__annotation_config_filename        = "annotation.conf"
__label_filename                    = 'labels.conf'
__kb_shortcut_filename              = 'kb_shortcuts.conf'

# fallback defaults if config files not found
__default_configuration = """
[entities]
Protein

[relations]
Equiv	Arg1:Protein, Arg2:Protein

[events]
Protein_binding|GO:0005515	Theme+:Protein
Gene_expression|GO:0010467	Theme:Protein

[attributes]
Negation	Arg:<EVENT>
Speculation	Arg:<EVENT>
"""

__default_labels = """
Protein\tProtein\tPro\tP
Protein_binding\tProtein binding\tBinding\tBind
Gene_expression\tGene_expression\tExpression\tExp
Theme\tTheme\tTh
"""

__default_kb_shortcuts = """
P	Protein
"""

# Reserved "macros" with special meanings in configuration.
reserved_macro_name   = ["ANY", "ENTITY", "RELATION", "EVENT", "NONE"]
reserved_macro_string = ["<%s>" % n for n in reserved_macro_name]

def normalize_to_storage_form(t):
    """
    Given a label, returns a form of the term that can be used for
    disk storage. For example, space can be replaced with underscores
    to allow use with space-separated formats.
    """
    if t not in normalize_to_storage_form.__cache:
        # conservative implementation: replace any space with
        # underscore, replace unicode accented characters with
        # non-accented equivalents, remove others, and finally replace
        # all characters not in [a-zA-Z0-9_-] with underscores.

        import re
        import unicodedata

        n = t.replace(" ", "_")
        if isinstance(n, unicode):
            ascii = unicodedata.normalize('NFKD', n).encode('ascii', 'ignore')
        n  = re.sub(r'[^a-zA-Z0-9_-]', '_', n)

        normalize_to_storage_form.__cache[t] = n

    return normalize_to_storage_form.__cache[t]
normalize_to_storage_form.__cache = {}

class TypeHierarchyNode:
    """
    Represents a node in a simple hierarchical ontology. 

    Each node is associated with a set of terms, one of which (the
    storage_form) matches the way in which the type denoted by the
    node is referenced to in data stored on disk and in client-server
    communications. This term is guaranteed to be in "storage form" as
    defined by normalize_to_storage_form().

    Each node may be associated with one or more "arguments", which
    are (multivalued) key:value pairs. These determine various characteristics 
    of the node, but their interpretation depends on the hierarchy the
    node occupies: for example, for events the arguments correspond to
    event arguments.
    """
    def __init__(self, terms, args):
        self.terms, self.args = terms, args

        if len(terms) == 0 or len([t for t in terms if t == ""]) != 0:
            Messager.debug("Empty term in type configuration" % (a, args), duration=-1)
            raise InvalidProjectConfigException

        # unused if any of the terms marked with "!"
        self.unused = False
        for i in range(len(self.terms)):
            if self.terms[i][0] == "!":
                self.terms[i]= self.terms[i][1:]
                self.unused = True
        self.children = []

        # The first of the listed terms is used as the primary term for
        # storage. Due to format restrictions, this form must not have
        # e.g. space or other forms.
        self.__primary_term = normalize_to_storage_form(self.terms[0])
        # TODO: this might not be the ideal place to put this warning
        if self.__primary_term != self.terms[0]:
            Messager.warning("Note: in configuration, term '%s' is not appropriate for storage (should match '^[a-zA-Z0-9_-]*$'), using '%s' instead. (Revise configuration file to get rid of this message. Terms other than the first are not subject to this restriction.)" % (self.terms[0], self.__primary_term), -1)
            pass

        # TODO: cleaner and more localized parsing
        self.arguments = {}
        self.arg_list = []
        self.mandatory_arguments = []
        self.multiple_allowed_arguments = []
        self.keys_by_type = {}
        for a in self.args:
            a = a.strip()
            m = re.match(r'^(.*?):(.*)$', a)
            if not m:
                Messager.warning("Project configuration: Failed to parse argument %s (args: %s)" % (a, args), 5)
                raise InvalidProjectConfigException
            key, atypes = m.groups()

            if key[-1:] not in ("?", "*"):
                mandatory_key = True
            else:
                mandatory_key = False

            if key[-1:] in ("*", "+"):
                multiple_allowed = True
            else:
                multiple_allowed = False

            if key[-1:] in ("?", "*", "+"):
                key = key[:-1]

            if key in self.arguments:
                Messager.warning("Project configuration: error parsing: %s argument '%s' appears multiple times." % key, 5)
                raise InvalidProjectConfigException

            self.arg_list.append(key)
            
            if mandatory_key:
                self.mandatory_arguments.append(key)

            if multiple_allowed:
                self.multiple_allowed_arguments.append(key)

            for atype in atypes.split("|"):
                if atype.strip() == "":
                    Messager.warning("Project configuration: error parsing: empty type for argument '%s'." % a, 5)
                    raise InvalidProjectConfigException

                # Check disabled; need to support arbitrary UTF values for attributes.conf.
                # TODO: consider checking for similar for appropriate confs.
#                 if atype not in reserved_macro_string and normalize_to_storage_form(atype) != atype:
#                     Messager.warning("Project configuration: '%s' is not a valid argument (should match '^[a-zA-Z0-9_-]*$')" % atype, 5)
#                     raise InvalidProjectConfigException

                if key not in self.arguments:
                    self.arguments[key] = []
                self.arguments[key].append(atype)

                if atype not in self.keys_by_type:
                    self.keys_by_type[atype] = []
                self.keys_by_type[atype].append(key)

    def storage_form(self):
        """
        Returns the form of the term used for storage serverside.
        """
        return self.__primary_term

def __read_term_hierarchy(input):
    root_nodes    = []
    last_node_at_depth = {}

    macros = {}
    for l in input:
        # skip empties and lines starting with '#'
        if l.strip() == '' or re.match(r'^\s*#', l):
            continue

        # interpret lines of only hyphens as separators
        # for display
        if re.match(r'^\s*-+\s*$', l):
            # TODO: proper placeholder and placing
            root_nodes.append("SEPARATOR")
            continue

        # interpret lines of the format <STR1>=STR2 as "macro"
        # definitions, defining <STR1> as a placeholder that should be
        # replaced with STR2 whevever it occurs.
        m = re.match(r'^<([a-zA-Z_-]+)>=\s*(.*?)\s*$', l)
        if m:
            name, value = m.groups()
            if name in reserved_macro_name:
                Messager.error("Cannot redefine <%s> in configuration, it is a reserved name." % name)
                # TODO: proper exception
                assert False
            else:
                macros["<%s>" % name] = value
            continue

        # macro expansion
        for n in macros:
            l = l.replace(n, macros[n])
        
        m = re.match(r'^(\s*)([^\t]+)(?:\t(.*))?$', l)
        assert m, "Error parsing line: '%s'" % l
        indent, terms, args = m.groups()
        terms = [t.strip() for t in terms.split("|") if t.strip() != ""]
        if args is None or args.strip() == "":
            args = []
        else:
            args = [a.strip() for a in args.split(",") if a.strip() != ""]

        # depth in the ontology corresponds to the number of
        # spaces in the initial indent.
        depth = len(indent)

        n = TypeHierarchyNode(terms, args)
        if depth == 0:
            # root level, no children assignments
            root_nodes.append(n)
        else:
            # assign as child of last node at the depth of the parent
            assert depth-1 in last_node_at_depth, "Error: no parent for '%s'" % l
            last_node_at_depth[depth-1].children.append(n)
        last_node_at_depth[depth] = n

    return root_nodes

def __read_or_default(filename, default):
    try:
        f = open_textfile(filename, 'r')
        r = f.read()
        f.close()
        return r
    except:
        # TODO: specific exception handling and reporting
        return default

def __parse_labels(labelstr, default, source):
    try:
        labels = {}
        for l in labelstr.split("\n"):
            l = l.rstrip()
            if l == "" or l[:1] == "#":
                continue
            fields = l.split("\t")
            if len(fields) < 2:
                Messager.warning("Note: failed to parse line in label configuration (ignoring; format is tab-separated, STORAGE_FORM\\tDISPLAY_FORM[\\tABBREV1\\tABBREV2...]): '%s'" % l, -1)
            else:
                storage_form, others = fields[0], fields[1:]
                normsf = normalize_to_storage_form(storage_form)
                if normsf != storage_form:
                    Messager.warning("Note: first field '%s' is not a valid storage form in label configuration (should match '^[a-zA-Z0-9_-]*$'; using '%s' instead): '%s'" % (storage_form, normsf, l), -1)
                    storage_form = normsf
                    
                if storage_form in labels:
                    Messager.warning("Note: '%s' defined multiple times in label configuration (only using last definition)'" % storage_form, -1)

                labels[storage_form] = others
    except:
        # TODO: specific exception handling
        Messager.warning("Project configuration: error parsing labels from %s. Configuration may be wrong." % source, 5)
        labels = default

    return labels

def __parse_kb_shortcuts(shortcutstr, default, source):
    try:
        shortcuts = {}
        for l in shortcutstr.split("\n"):
            l = l.strip()
            if l == "" or l[:1] == "#":
                continue
            key, type = re.split(r'[ \t]+', l)
            if key in shortcuts:
                Messager.warning("Project configuration: keyboard shortcut for '%s' defined multiple times. Ignoring all but first ('%s')" % (key, shortcuts[key]))
            else:
                shortcuts[key] = type
    except:
        # TODO: specific exception handling
        Messager.warning("Project configuration: error parsing keyboard shortcuts from %s. Configuration may be wrong." % source, 5)
        shortcuts = default
    return shortcuts

def get_config_path(directory):
    return __read_first_in_directory_tree(directory, __annotation_config_filename)[1]

def __read_first_in_directory_tree(directory, filename):
    # config will not be available command-line invocations;
    # in these cases search whole tree
    try:
        from config import BASE_DIR
    except:
        BASE_DIR = "/"
    from os.path import split, join

    source, result = None, None

    # check from the given directory and parents, but not above BASE_DIR
    if directory is not None:
        # TODO: this check may fail; consider "foo//bar/data"
        while BASE_DIR in directory:
            source = join(directory, filename)
            result = __read_or_default(source, None)
            if result is not None:
                break
            directory = split(directory)[0]

    return (result, source)

def __parse_configs(configstr, source):
    # top-level config structure is a set of term hierarchies
    # separated by lines consisting of "[SECTION]" where SECTION is
    # e.g.  "entities", "relations", etc.

    # start by splitting config file lines by section

    section = "general"
    section_lines = { section: [] }
    for ln, l in enumerate(configstr.split("\n")):
        m = re.match(r'^\s*\[(.*)\]\s*$', l)
        if m:
            section = m.group(1)
            if section not in expected_configuration_sections:
                Messager.warning("Project configuration: unexpected section [%s] in %s. Ignoring contents." % (section, source), 5)
            if section not in section_lines:
                section_lines[section] = []
        else:
            section_lines[section].append(l)

    # attempt to parse lines in each section as a term hierarchy
    configs = {}
    for s, sl in section_lines.items():
        try:
            configs[s] = __read_term_hierarchy(sl)
        except:
            Messager.warning("Project configuration: error parsing section [%s] in %s." % (s, source), 5)
            raise

    # verify that expected sections are present; replace with empty if not.
    for s in expected_configuration_sections:
        if s not in configs:
            Messager.warning("Project configuration: missing section [%s] in %s. Configuration may be wrong." % (s, source), 5)
            configs[s] = []

    return configs

# final fallback for configuration; a minimal known-good config
__minimal_configuration = {
    ENTITY_SECTION    : [TypeHierarchyNode(["Protein"], [])],
    RELATION_SECTION  : [TypeHierarchyNode(["Equiv"], ["Arg1:Protein", "Arg2:Protein"])],
    EVENT_SECTION     : [TypeHierarchyNode(["Event"], ["Theme:Protein"])],
    ATTRIBUTE_SECTION : [TypeHierarchyNode(["Negation"], ["Arg:<EVENT>"])],
    }
            
def get_configs(directory, filename):
    if (directory, filename) not in get_configs.__cache:
        configstr, source =  __read_first_in_directory_tree(directory, filename)

        if configstr is None:
            # didn't get one; try default dir and fall back to the default
            configstr = __read_or_default(filename, __default_configuration)
            if configstr == __default_configuration:                
                Messager.info("Project configuration: no configuration file (%s) found, using default." % filename, 5)
                source = "[default]"
            else:
                source = filename

        # try to parse what was found, fall back to minimal config
        try: 
            configs = __parse_configs(configstr, source)        
        except:
            Messager.warning("Project configuration: Falling back to minimal default. Configuration is likely wrong.", 5)
            configs = __minimal_configuration

        get_configs.__cache[(directory, filename)] = configs

    return get_configs.__cache[(directory, filename)]
get_configs.__cache = {}

def __get_labels(directory, filename, default_labels, min_labels):

    labelstr, source = __read_first_in_directory_tree(directory, filename)

    if labelstr is None:
        labelstr = __read_or_default(filename, default_labels)
        if labelstr == default_labels:
            source = "[default labels]"
        else:
            source = filename

    labels = __parse_labels(labelstr, min_labels, source)
    return labels

def __get_kb_shortcuts(directory, filename, default_shortcuts, min_shortcuts):

    shortcutstr, source = __read_first_in_directory_tree(directory, filename)

    if shortcutstr is None:
        shortcutstr = __read_or_default(filename, default_shortcuts)
        if shortcutstr == default_shortcuts:
            source = "[default kb_shortcuts]"
        else:
            source = filename

    kb_shortcuts = __parse_kb_shortcuts(shortcutstr, min_shortcuts, source)
    return kb_shortcuts

def get_entity_type_hierarchy(directory):    
    return get_configs(directory, __annotation_config_filename)[ENTITY_SECTION]

def get_relation_type_hierarchy(directory):    
    return get_configs(directory, __annotation_config_filename)[RELATION_SECTION]

def get_event_type_hierarchy(directory):    
    return get_configs(directory, __annotation_config_filename)[EVENT_SECTION]

def get_attribute_type_hierarchy(directory):    
    return get_configs(directory, __annotation_config_filename)[ATTRIBUTE_SECTION]

def get_labels(directory):
    cache = get_labels.__cache
    if directory not in cache:
        a = __get_labels(directory,
                         __label_filename,
                         __default_labels,
                         { "Protein" : [ "Protein", "Pro", "P" ], 
                           "Theme" : [ "Theme", "Th" ] }
                         )
        cache[directory] = a

    return cache[directory]
get_labels.__cache = {}

def get_kb_shortcuts(directory):
    cache = get_kb_shortcuts.__cache
    if directory not in cache:
        a = __get_kb_shortcuts(directory,
                                __kb_shortcut_filename,
                                __default_kb_shortcuts,
                               { "P" : "Positive_regulation" })
        cache[directory] = a

    return cache[directory]
get_kb_shortcuts.__cache = {}

def __collect_type_list(node, collected):
    if node == "SEPARATOR":
        return collected

    collected.append(node)

    for c in node.children:
        __collect_type_list(c, collected)

    return collected

def __type_hierarchy_to_list(hierarchy):
    root_nodes = hierarchy
    types = []
    for n in root_nodes:
        __collect_type_list(n, types)
    return types

# TODO: it's not clear it makes sense for all of these methods to have
# their own caches; this seems a bit like a case of premature
# optimization to me. Consider simplifying.

def get_entity_type_list(directory):
    cache = get_entity_type_list.__cache
    if directory not in cache:
        cache[directory] = __type_hierarchy_to_list(get_entity_type_hierarchy(directory))
    return cache[directory]
get_entity_type_list.__cache = {}

def get_event_type_list(directory):
    cache = get_event_type_list.__cache
    if directory not in cache:
        cache[directory] = __type_hierarchy_to_list(get_event_type_hierarchy(directory))
    return cache[directory]
get_event_type_list.__cache = {}

def get_relation_type_list(directory):
    cache = get_relation_type_list.__cache
    if directory not in cache:
        cache[directory] = __type_hierarchy_to_list(get_relation_type_hierarchy(directory))
    return cache[directory]
get_relation_type_list.__cache = {}

def get_attribute_type_list(directory):
    cache = get_attribute_type_list.__cache
    if directory not in cache:
        cache[directory] = __type_hierarchy_to_list(get_attribute_type_hierarchy(directory))
    return cache[directory]
get_attribute_type_list.__cache = {}    

def get_node_by_storage_form(directory, term):
    cache = get_node_by_storage_form.__cache
    if directory not in cache:
        d = {}
        for e in get_entity_type_list(directory) + get_event_type_list(directory):
            t = e.storage_form()
            if t in d:
                Messager.warning("Project configuration: interface term %s matches multiple types (incl. '%s' and '%s'). Configuration may be wrong." % (t, d[t].storage_form(), e.storage_form()), 5)
            d[t] = e
        cache[directory] = d

    return cache[directory].get(term, None)
get_node_by_storage_form.__cache = {}

def __directory_relations_by_arg_num(directory, num, atype):
    assert num >= 0 and num < 2, "INTERNAL ERROR"

    rels = []

    for r in get_relation_type_list(directory):
        # "Special" nesting relation ignore in regular relation listings
        # TODO: avoid magic string value
        if r.storage_form() == "ENTITY-NESTING":
            continue

        if len(r.arg_list) != 2:
            Messager.warning("Relation type %s has %d arguments in configuration (%s; expected 2). Please fix configuration." % (r.storage_form(), len(args), ",".join(args)))
        else:
            types = r.arguments[r.arg_list[num]]
            for type in types:
                # TODO: "wildcards" other than <ANY>
                if type == "<ANY>" or atype == "<ANY>" or type == atype:
                    rels.append(r)

    return rels

def get_relations_by_arg1(directory, atype):
    cache = get_relations_by_arg1.__cache
    cache[directory] = cache.get(directory, {})
    if atype not in cache[directory]:
        cache[directory][atype] = __directory_relations_by_arg_num(directory, 0, atype)
    return cache[directory][atype]
get_relations_by_arg1.__cache = {}

def get_relations_by_arg2(directory, atype):
    cache = get_relations_by_arg2.__cache
    cache[directory] = cache.get(directory, {})
    if atype not in cache[directory]:
        cache[directory][atype] = __directory_relations_by_arg_num(directory, 1, atype)
    return cache[directory][atype]
get_relations_by_arg2.__cache = {}

def get_labels_by_storage_form(directory, term):
    cache = get_labels_by_storage_form.__cache
    if directory not in cache:
        cache[directory] = {}
        for l, labels in get_labels(directory).items():
            cache[directory][l] = labels
    return cache[directory].get(term, None)
get_labels_by_storage_form.__cache = {}

# fallback for missing or partial config: these are highly likely to
# be entity (as opposed to an event or relation) types.
# TODO: remove this workaround once the configs stabilize.
very_likely_physical_entity_types = [
    'Protein',
    'Entity',
    'Organism',
    'Chemical',
    'Two-component-system',
    'Regulon-operon',
    # for more PTM annotation
    'Protein_family_or_group',
    'DNA_domain_or_region',
    'Protein_domain_or_region',
    'Amino_acid_monomer',
    'Carbohydrate',
    # for AZ corpus
    'Cell_type',
    'Drug_or_compound',
    'Gene_or_gene_product',
    'Pathway',
    'Tissue',
    #'Not_sure',
    #'Other',
    'Other_pharmaceutical_agent',
    ]

# helper; doesn't really belong here
# TODO: shouldn't we have an utils.py or something for stuff like this? 
def unique_preserve_order(iterable):
    seen = set()
    uniqued = []
    for i in iterable:
        if i not in seen:
            seen.add(i)
            uniqued.append(i)
    return uniqued

class ProjectConfiguration(object):
    def __init__(self, directory):
        # debugging (note: latter test for windows paths)
        if directory[:1] != "/" and not re.search(r'^[a-zA-Z]:\\', directory):
            Messager.debug("Project config received relative directory ('%s'), configuration may not be found." % directory, duration=-1)
        self.directory = directory

    def mandatory_arguments(self, type):
        """
        Returns the mandatory argument types that must be present for
        an annotation of the given type.
        """
        node = get_node_by_storage_form(self.directory, type)
        if node is None:
            Messager.warning("Project configuration: unknown type %s. Configuration may be wrong." % type)
            return []
        return node.mandatory_arguments

    def multiple_allowed_arguments(self, type):
        """
        Returns the argument types that are allowed to be filled more
        than once for an annotation of the given type.
        """
        node = get_node_by_storage_form(self.directory, type)
        if node is None:
            Messager.warning("Project configuration: unknown type %s. Configuration may be wrong." % type)
            return []
        return node.multiple_allowed_arguments

    def arc_types_from(self, from_ann):
        return self.arc_types_from_to(from_ann)

    def relation_types_from(self, from_ann):
        """
        Returns the possible relation types that can have an
        annotation of the given type as their arg1.
        """
        return [r.storage_form() for r in get_relations_by_arg1(self.directory, from_ann)]

    def relation_types_to(self, to_ann):
        """
        Returns the possible relation types that can have an
        annotation of the given type as their arg2.
        """
        return [r.storage_form() for r in get_relations_by_arg2(self.directory, to_ann)]

    def relation_types_from_to(self, from_ann, to_ann):
        """
        Returns the possible relation types that can have the
        given arg1 and arg2.
        """
        types = []

        t1r = get_relations_by_arg1(self.directory, from_ann)
        t2r = get_relations_by_arg2(self.directory, to_ann)

        for r in t1r:
            if r in t2r:
                types.append(r.storage_form())

        return types

    def arc_types_from_to(self, from_ann, to_ann="<ANY>"):
        """
        Returns the possible arc types that can connect an annotation
        of type from_ann to an annotation of type to_ann.
        If to_ann has the value \"<ANY>\", returns all possible arc types.
        """

        from_node = get_node_by_storage_form(self.directory, from_ann)

        if from_node is None:
            Messager.warning("Project configuration: unknown type %s. Configuration may be wrong." % from_ann)
            return []

        if to_ann == "<ANY>":
            relations_from = get_relations_by_arg1(self.directory, from_ann)
            return unique_preserve_order([role for role in from_node.arguments] + [r.storage_form() for r in relations_from])

        # specific hits
        types = from_node.keys_by_type.get(to_ann, [])

        if "<ANY>" in from_node.keys_by_type:
            types += from_node.keys_by_type["<ANY>"]

        # generic arguments
        if self.is_event_type(to_ann) and '<EVENT>' in from_node.keys_by_type:
            types += from_node.keys_by_type['<EVENT>']
        if self.is_physical_entity_type(to_ann) and '<ENTITY>' in from_node.keys_by_type:
            types += from_node.keys_by_type['<ENTITY>']

        # relations
        types.extend(self.relation_types_from_to(from_ann, to_ann))

        return unique_preserve_order(types)

    def get_labels(self):
        return get_labels(self.directory)

    def get_kb_shortcuts(self):
        return get_kb_shortcuts(self.directory)

    def get_attribute_types(self):
        return [t.storage_form() for t in get_attribute_type_list(self.directory)]

    def get_event_types(self):
        return [t.storage_form() for t in get_event_type_list(self.directory)]

    def get_relation_types(self):
        return [t.storage_form() for t in get_relation_type_list(self.directory)]        

    def get_relation_by_type(self, type):
        for r in get_relation_type_list(self.directory):
            if r.storage_form() == type:
                return r
        return None

    def get_entity_types(self):
        return [t.storage_form() for t in get_entity_type_list(self.directory)]

    def get_entity_type_hierarchy(self):
        return get_entity_type_hierarchy(self.directory)

    def get_relation_type_hierarchy(self):
        return get_relation_type_hierarchy(self.directory)

    def get_event_type_hierarchy(self):
        return get_event_type_hierarchy(self.directory)

    def get_attribute_type_hierarchy(self):
        return get_attribute_type_hierarchy(self.directory)

    def preferred_display_form(self, t):
        """
        Given a storage form label, returns the preferred display form
        as defined by the label configuration (labels.conf)
        """
        labels = get_labels_by_storage_form(self.directory, t)
        if labels is None or len(labels) < 1:
            return t
        else:
            return labels[0]

    def is_physical_entity_type(self, t):
        # TODO: remove this temporary hack
        if t in very_likely_physical_entity_types:
            return True
        return t in self.get_entity_types()

    def is_event_type(self, t):
        return t in self.get_event_types()

    def is_relation_type(self, t):
        return t is self.get_relation_types()

    def type_category(self, t):
        """
        Returns the category of the given type t.
        The categories can be compared for equivalence but offer
        no other interface.
        """
        if self.is_physical_entity_type(t):
            return "PHYSICAL"
        elif self.is_event_type(t):
            return "EVENT"
        elif self.is_relation_type(t):
            return "RELATION"
        else:
            # TODO:
            return "OTHER"
